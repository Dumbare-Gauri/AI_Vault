import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import DashboardSnapshot


class DashboardSnapshotRepository:
    """Append-only — one row per `RecommendationJob` run, giving the
    dashboard's trend charts a natural history with no separate
    time-series store (Phase 7 spec: "keep historical data so trends can
    be visualized over time")."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID | None = None,
        recommendation_job_id: uuid.UUID | None,
        connected_providers: int,
        total_files: int,
        total_folders: int,
        total_storage_bytes: int,
        classified_files: int,
        unclassified_files: int,
        pending_enrichment_files: int,
        embedded_files: int,
        relationship_count: int,
        active_recommendations: int,
        knowledge_completeness_score: float,
    ) -> DashboardSnapshot:
        snapshot = DashboardSnapshot(
            organization_id=organization_id,
            connector_id=connector_id,
            recommendation_job_id=recommendation_job_id,
            connected_providers=connected_providers,
            total_files=total_files,
            total_folders=total_folders,
            total_storage_bytes=total_storage_bytes,
            classified_files=classified_files,
            unclassified_files=unclassified_files,
            pending_enrichment_files=pending_enrichment_files,
            embedded_files=embedded_files,
            relationship_count=relationship_count,
            active_recommendations=active_recommendations,
            knowledge_completeness_score=knowledge_completeness_score,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get_latest_for_organization(self, organization_id: uuid.UUID) -> DashboardSnapshot | None:
        return (
            self._session.query(DashboardSnapshot)
            .filter_by(organization_id=organization_id)
            .order_by(DashboardSnapshot.created_at.desc())
            .first()
        )

    def list_recent_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 30, connector_id: uuid.UUID | None = None
    ) -> list[DashboardSnapshot]:
        """Newest-first — callers that render a trend chart should reverse
        this to chronological order themselves. With `connector_id`, only
        that storage's snapshots."""
        query = self._session.query(DashboardSnapshot).filter_by(organization_id=organization_id)
        if connector_id is not None:
            query = query.filter(DashboardSnapshot.connector_id == connector_id)
        return query.order_by(DashboardSnapshot.created_at.desc()).limit(limit).all()
