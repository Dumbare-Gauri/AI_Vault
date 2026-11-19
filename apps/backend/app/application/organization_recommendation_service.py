import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.organization_producer import (
    enqueue_organization_analysis_job,
    enqueue_organization_recommendation_apply,
)
from vault_shared import ConflictError, NotFoundError, ValidationError
from vault_shared.db.models import (
    OrganizationAnalysisJob,
    OrganizationRecommendation,
    OrganizationRecommendationStatus,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    ConnectorCredentialsRepository,
    FileRepository,
    OrganizationAnalysisJobRepository,
    OrganizationRecommendationRepository,
)
from vault_shared.execution.permission_validation import validate_execution_permissions


class OrganizationRecommendationService:
    """Read surface plus the two user-triggered actions for the
    Organization Recommendation Engine (spec's "Organize my Drive"
    workflow): running a fresh analysis pass, and applying one proposed
    recommendation. Both actions only validate and enqueue here — the
    real work (entity inference, lifecycle scoring, recommendation
    generation, and the actual folder-create/move against Drive) runs in
    `apps/worker`, same division of labor `RecommendationService.
    trigger_refresh` already uses for the plain Recommendation Engine."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._recommendations = OrganizationRecommendationRepository(db)
        self._analysis_jobs = OrganizationAnalysisJobRepository(db)
        self._audit_logs = AuditLogRepository(db)
        self._files = FileRepository(db)
        self._credentials = ConnectorCredentialsRepository(db)

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[OrganizationRecommendation]:
        return self._recommendations.list_for_organization(
            organization_id, status=status, kind=kind
        )

    def get_owned(
        self, recommendation_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> OrganizationRecommendation:
        recommendation = self._recommendations.get_owned(
            recommendation_id, organization_id=organization_id
        )
        if recommendation is None:
            raise NotFoundError("Organization recommendation not found.")
        return recommendation

    def trigger_analysis(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationAnalysisJob:
        if self._analysis_jobs.has_active_job(organization_id):
            raise ConflictError(
                "An organization analysis is already pending or running for this organization."
            )

        job = self._analysis_jobs.create(
            organization_id=organization_id, triggered_by_user_id=user_id
        )
        self._audit_logs.record(
            event_type="organization_analysis_triggered",
            organization_id=organization_id,
            user_id=user_id,
        )
        self._db.commit()

        enqueue_organization_analysis_job(job.id)
        return job

    def trigger_apply(
        self, recommendation_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationRecommendation:
        recommendation = self.get_owned(recommendation_id, organization_id=organization_id)
        if recommendation.status != OrganizationRecommendationStatus.ACTIVE:
            raise ConflictError(f"This recommendation is already {recommendation.status}.")
        self._check_write_permissions(recommendation, organization_id=organization_id)

        self._audit_logs.record(
            event_type="organization_recommendation_apply_triggered",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"organization_recommendation_id": str(recommendation_id)},
        )
        self._db.commit()

        enqueue_organization_recommendation_apply(
            recommendation_id, organization_id=organization_id, user_id=user_id
        )
        return recommendation

    def _check_write_permissions(
        self, recommendation: OrganizationRecommendation, *, organization_id: uuid.UUID
    ) -> None:
        """Same synchronous DB-only pre-check `ExecutionPlanService.
        _finalize_plan` runs for every direct plan, so a missing connection
        or write scope is an immediate error the user sees, not a silent
        worker-side refusal after "Applying now". The worker re-checks
        right before mutating anyway (`ExecutionService.create_folder`)."""
        if not recommendation.affected_file_ids:
            raise ValidationError("This recommendation has no affected files.")
        row = self._files.get_owned_with_connector(
            uuid.UUID(recommendation.affected_file_ids[0]), organization_id=organization_id
        )
        if row is None:
            raise ValidationError(
                "This recommendation's files could not be found — it may be stale. "
                "Re-analyze and try again."
            )
        _file, connector = row
        failures = validate_execution_permissions(
            connector=connector, credentials=self._credentials.get_by_connector_id(connector.id)
        )
        if failures:
            raise ValidationError("Cannot apply — " + "; ".join(failures))
