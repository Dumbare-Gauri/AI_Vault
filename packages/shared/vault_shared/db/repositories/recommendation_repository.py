import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import Recommendation, RecommendationStatus


class RecommendationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, recommendation_id: uuid.UUID) -> Recommendation | None:
        return self._session.get(Recommendation, recommendation_id)

    def get_owned(
        self, recommendation_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> Recommendation | None:
        return (
            self._session.query(Recommendation)
            .filter_by(id=recommendation_id, organization_id=organization_id)
            .first()
        )

    def get_by_rule(
        self,
        *,
        organization_id: uuid.UUID,
        rule_name: str,
        connector_id: uuid.UUID | None = None,
    ) -> Recommendation | None:
        """A rule fires once per storage. Without `connector_id` (the
        Automation Engine, which works across the organization) this is the
        highest-priority active one, never a combination of storages."""
        query = self._session.query(Recommendation).filter_by(
            organization_id=organization_id, rule_name=rule_name
        )
        if connector_id is not None:
            return query.filter(Recommendation.connector_id == connector_id).first()
        return (
            query.filter(
                Recommendation.connector_id.isnot(None),
                Recommendation.status == RecommendationStatus.ACTIVE,
            )
            .order_by(Recommendation.priority_score.desc())
            .first()
        )

    def upsert(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        recommendation_job_id: uuid.UUID,
        rule_name: str,
        category: str,
        title: str,
        description: str,
        confidence: float,
        estimated_impact: str,
        impact_value: float | None,
        risk_level: str,
        suggested_action: str,
        related_departments: list[str],
        affected_file_ids: list[str],
        priority_score: float,
    ) -> Recommendation:
        """A rule fired this run — create its recommendation row, or revive/
        refresh the existing one for `(organization_id, rule_name)` if it
        was previously `RESOLVED` (the condition came back) or still
        `ACTIVE` (the condition persists, fields just refreshed with this
        run's numbers). Never creates a second row for the same rule —
        `(organization_id, rule_name)` is a unique constraint, so this is
        what makes a rule's recommendation recomputable across runs
        instead of accumulating duplicates (Phase 7 spec: "Recomputable
        after new scans")."""
        recommendation = self.get_by_rule(
            organization_id=organization_id, rule_name=rule_name, connector_id=connector_id
        )
        if recommendation is None:
            recommendation = Recommendation(
                organization_id=organization_id, connector_id=connector_id, rule_name=rule_name
            )
            self._session.add(recommendation)

        recommendation.recommendation_job_id = recommendation_job_id
        recommendation.category = category
        recommendation.title = title
        recommendation.description = description
        recommendation.confidence = confidence
        recommendation.estimated_impact = estimated_impact
        recommendation.impact_value = impact_value
        recommendation.risk_level = risk_level
        recommendation.suggested_action = suggested_action
        recommendation.related_departments = related_departments
        recommendation.affected_file_ids = affected_file_ids
        recommendation.priority_score = priority_score
        recommendation.status = RecommendationStatus.ACTIVE
        recommendation.resolved_at = None
        self._session.flush()
        return recommendation

    def resolve_stale(
        self, *, organization_id: uuid.UUID, connector_id: uuid.UUID, seen_rule_names: set[str]
    ) -> None:
        """Any rule that didn't fire this run means its underlying
        condition no longer holds — mark its previously `ACTIVE`
        recommendation `RESOLVED` rather than deleting it, preserving
        history for trend analysis (Phase 7 spec)."""
        query = self._session.query(Recommendation).filter(
            Recommendation.organization_id == organization_id,
            Recommendation.connector_id == connector_id,
            Recommendation.status == RecommendationStatus.ACTIVE,
        )
        if seen_rule_names:
            query = query.filter(~Recommendation.rule_name.in_(seen_rule_names))
        stale = query.all()
        now = datetime.now(UTC)
        for recommendation in stale:
            recommendation.status = RecommendationStatus.RESOLVED
            recommendation.resolved_at = now
        self._session.flush()

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
        exclude_categories: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        connector_id: uuid.UUID | None = None,
    ) -> list[Recommendation]:
        query = self._session.query(Recommendation).filter_by(organization_id=organization_id)
        if connector_id is not None:
            query = query.filter(Recommendation.connector_id == connector_id)
        if category is not None:
            query = query.filter(Recommendation.category == category)
        if status is not None:
            query = query.filter(Recommendation.status == status)
        if exclude_categories:
            query = query.filter(~Recommendation.category.in_(exclude_categories))
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                Recommendation.title.ilike(pattern) | Recommendation.description.ilike(pattern)
            )
        return (
            query.order_by(Recommendation.priority_score.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_active_for_organization(
        self, organization_id: uuid.UUID, *, connector_id: uuid.UUID | None = None
    ) -> int:
        query = self._session.query(Recommendation).filter_by(
            organization_id=organization_id, status=RecommendationStatus.ACTIVE
        )
        if connector_id is not None:
            query = query.filter(Recommendation.connector_id == connector_id)
        return query.count()
