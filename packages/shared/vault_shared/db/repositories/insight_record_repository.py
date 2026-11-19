import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import InsightRecord


class InsightRecordRepository:
    """Append-only — insights are observational snapshots, not open issues
    to resolve, so there's no update/upsert method here (contrast with
    `RecommendationRepository.upsert`)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID | None = None,
        recommendation_job_id: uuid.UUID | None,
        insight_type: str,
        title: str,
        description: str,
        confidence: float,
        related_file_ids: list[str],
    ) -> InsightRecord:
        insight = InsightRecord(
            organization_id=organization_id,
            connector_id=connector_id,
            recommendation_job_id=recommendation_job_id,
            insight_type=insight_type,
            title=title,
            description=description,
            confidence=confidence,
            related_file_ids=related_file_ids,
        )
        self._session.add(insight)
        self._session.flush()
        return insight

    def list_recent_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 20, connector_id: uuid.UUID | None = None
    ) -> list[InsightRecord]:
        """With `connector_id`, only that storage's insights."""
        query = self._session.query(InsightRecord).filter_by(organization_id=organization_id)
        if connector_id is not None:
            query = query.filter(InsightRecord.connector_id == connector_id)
        return query.order_by(InsightRecord.created_at.desc()).limit(limit).all()
