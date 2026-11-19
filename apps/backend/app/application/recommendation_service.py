import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.recommendation_producer import enqueue_recommendation_job
from vault_shared import ConflictError, ForbiddenError, NotFoundError
from vault_shared.db.models import (
    Recommendation,
    RecommendationCategory,
    RecommendationJob,
    RecommendationTrigger,
    RoleName,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    RecommendationJobRepository,
    RecommendationRepository,
)

_AUTHORIZED_FOR_SECURITY = (RoleName.OWNER, RoleName.ADMIN)


class RecommendationService:
    """Read surface for the Recommendation Center (Phase 7 spec) plus the
    manual "refresh recommendations" trigger — mirrors `FileService`'s
    read-only role (the worker's `RecommendationService` does the actual
    computation; this one only reads what it wrote and enqueues new runs).

    Security requirement from the phase spec ("Sensitive recommendations
    should only be visible to authorized roles"): `SECURITY`-category
    recommendations are filtered out of list results for anyone who isn't
    an owner/admin, and a direct detail request for one raises
    `ForbiddenError` rather than silently 404ing — the caller should know
    *why* they can't see it, unlike a genuine cross-organization access
    attempt."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._recommendations = RecommendationRepository(db)
        self._jobs = RecommendationJobRepository(db)
        self._audit_logs = AuditLogRepository(db)

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        role: str,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
        connector_id: uuid.UUID | None = None,
    ) -> list[Recommendation]:
        exclude_categories = (
            None if role in _AUTHORIZED_FOR_SECURITY else [RecommendationCategory.SECURITY]
        )
        return self._recommendations.list_for_organization(
            organization_id,
            category=category,
            status=status,
            search=search,
            exclude_categories=exclude_categories,
            connector_id=connector_id,
        )

    def get_owned(
        self,
        recommendation_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        connector_id: uuid.UUID | None = None,
    ) -> Recommendation:
        recommendation = self._recommendations.get_owned(
            recommendation_id, organization_id=organization_id
        )
        if recommendation is None or (
            connector_id is not None and recommendation.connector_id != connector_id
        ):
            raise NotFoundError("Recommendation not found.")
        is_hidden_security_item = (
            recommendation.category == RecommendationCategory.SECURITY
            and role not in _AUTHORIZED_FOR_SECURITY
        )
        if is_hidden_security_item:
            raise ForbiddenError("You do not have permission to view this recommendation.")

        self._audit_logs.record(
            event_type="recommendation_viewed",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"recommendation_id": str(recommendation_id)},
        )
        self._db.commit()
        return recommendation

    def trigger_refresh(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> RecommendationJob:
        if self._jobs.has_active_job(organization_id):
            raise ConflictError(
                "A recommendation run is already pending or running for this organization."
            )

        job = self._jobs.create(
            organization_id=organization_id,
            triggered_by=RecommendationTrigger.MANUAL,
            triggered_by_user_id=user_id,
        )
        self._audit_logs.record(
            event_type="recommendation_refresh_triggered",
            organization_id=organization_id,
            user_id=user_id,
        )
        self._db.commit()

        enqueue_recommendation_job(job.id)
        return job
