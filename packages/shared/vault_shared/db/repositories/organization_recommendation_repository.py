import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vault_shared.db.models import OrganizationRecommendation, OrganizationRecommendationStatus


class OrganizationRecommendationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_owned(
        self, recommendation_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> OrganizationRecommendation | None:
        return (
            self._session.query(OrganizationRecommendation)
            .filter_by(id=recommendation_id, organization_id=organization_id)
            .first()
        )

    def get_active_for_entity(self, entity_id: uuid.UUID) -> OrganizationRecommendation | None:
        return (
            self._session.query(OrganizationRecommendation)
            .filter_by(entity_id=entity_id, status=OrganizationRecommendationStatus.ACTIVE)
            .first()
        )

    def upsert_for_entity(
        self,
        *,
        organization_id: uuid.UUID,
        entity_id: uuid.UUID,
        kind: str,
        title: str,
        reasoning_summary: str,
        evidence: list[dict],
        confidence: float,
        affected_file_ids: list[str],
        current_locations: list[dict],
        suggested_destination: list[str],
        estimated_storage_impact_bytes: int,
    ) -> OrganizationRecommendation:
        """One active recommendation per entity at a time — a re-run that
        still finds the entity scattered refreshes the existing row
        in place (same rationale as `Recommendation.upsert`) rather than
        accumulating repeats; an `APPLIED`/`REJECTED` row is left alone
        and a fresh `ACTIVE` one is created instead, since those are
        historical facts about a decision already made."""
        recommendation = self.get_active_for_entity(entity_id)
        if recommendation is None:
            recommendation = OrganizationRecommendation(
                organization_id=organization_id, entity_id=entity_id
            )
            self._session.add(recommendation)

        recommendation.kind = kind
        recommendation.title = title
        recommendation.reasoning_summary = reasoning_summary
        recommendation.evidence = evidence
        recommendation.confidence = confidence
        recommendation.affected_file_ids = affected_file_ids
        recommendation.current_locations = current_locations
        recommendation.suggested_destination = suggested_destination
        recommendation.estimated_storage_impact_bytes = estimated_storage_impact_bytes
        recommendation.status = OrganizationRecommendationStatus.ACTIVE
        self._session.flush()
        return recommendation

    def list_active_of_kind(
        self, organization_id: uuid.UUID, kind: str
    ) -> list[OrganizationRecommendation]:
        return (
            self._session.query(OrganizationRecommendation)
            .filter_by(
                organization_id=organization_id,
                kind=kind,
                status=OrganizationRecommendationStatus.ACTIVE,
            )
            .all()
        )

    def upsert_for_file(
        self,
        *,
        organization_id: uuid.UUID,
        file_id: uuid.UUID,
        kind: str,
        title: str,
        reasoning_summary: str,
        evidence: list[dict],
        confidence: float,
        current_locations: list[dict],
        suggested_destination: list[str],
        estimated_storage_impact_bytes: int,
    ) -> OrganizationRecommendation:
        """One active per-file recommendation of a kind at a time, refreshed
        in place on re-analysis — the per-file counterpart of
        `upsert_for_entity`."""
        recommendation = next(
            (
                candidate
                for candidate in self.list_active_of_kind(organization_id, kind)
                if candidate.affected_file_ids == [str(file_id)]
            ),
            None,
        )
        if recommendation is None:
            recommendation = OrganizationRecommendation(organization_id=organization_id)
            self._session.add(recommendation)

        recommendation.kind = kind
        recommendation.title = title
        recommendation.reasoning_summary = reasoning_summary
        recommendation.evidence = evidence
        recommendation.confidence = confidence
        recommendation.affected_file_ids = [str(file_id)]
        recommendation.current_locations = current_locations
        recommendation.suggested_destination = suggested_destination
        recommendation.estimated_storage_impact_bytes = estimated_storage_impact_bytes
        recommendation.status = OrganizationRecommendationStatus.ACTIVE
        self._session.flush()
        return recommendation

    def mark_stale(self, recommendation: OrganizationRecommendation) -> None:
        """The entity it was generated for is no longer scattered (the user
        consolidated it manually, or a previous `apply()` already moved
        everything) — mirrors `Recommendation.resolve_stale`'s soft-resolve,
        never a delete, so the recommendation remains visible for history."""
        recommendation.status = OrganizationRecommendationStatus.STALE
        self._session.flush()

    def mark_applied(
        self, recommendation: OrganizationRecommendation, *, execution_plan_id: uuid.UUID
    ) -> None:
        recommendation.status = OrganizationRecommendationStatus.APPLIED
        recommendation.execution_plan_id = execution_plan_id
        self._session.flush()

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[OrganizationRecommendation]:
        query = self._session.query(OrganizationRecommendation).filter_by(
            organization_id=organization_id
        )
        if status is not None:
            query = query.filter(OrganizationRecommendation.status == status)
        if kind is not None:
            query = query.filter(OrganizationRecommendation.kind == kind)
        return query.order_by(OrganizationRecommendation.confidence.desc()).all()

    def list_active_for_files(
        self, organization_id: uuid.UUID, file_ids: list[uuid.UUID]
    ) -> list[OrganizationRecommendation]:
        """Every active recommendation that touches at least one of
        `file_ids` — used by the organizational-memory `CORRECTION` hook
        in `ExecutionPlanService.create_ad_hoc_plan` to detect when a user
        moves a file somewhere other than what was suggested. Filtered in
        Python since `affected_file_ids` is a plain JSONB array, not a
        join table (same tradeoff `Recommendation.affected_file_ids` already
        makes)."""
        file_id_strings = {str(file_id) for file_id in file_ids}
        candidates = (
            self._session.query(OrganizationRecommendation)
            .filter_by(
                organization_id=organization_id,
                status=OrganizationRecommendationStatus.ACTIVE,
            )
            .all()
        )
        return [
            recommendation
            for recommendation in candidates
            if file_id_strings & set(recommendation.affected_file_ids)
        ]

    def count_active_for_organization(self, organization_id: uuid.UUID) -> int:
        return self._session.execute(
            select(func.count())
            .select_from(OrganizationRecommendation)
            .where(
                OrganizationRecommendation.organization_id == organization_id,
                OrganizationRecommendation.status == OrganizationRecommendationStatus.ACTIVE,
            )
        ).scalar_one()
