import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from vault_shared.db.models import DashboardSnapshot, File, InsightRecord, StorageConnector
from vault_shared.db.repositories import (
    DashboardSnapshotRepository,
    EmbeddingJobRepository,
    EnrichmentJobRepository,
    FileIntelligenceRepository,
    FileLifecycleRepository,
    FileRepository,
    InsightRecordRepository,
    OrganizationEntityRepository,
    OrganizationRecommendationRepository,
    RecommendationJobRepository,
    ScanJobRepository,
    StorageConnectorRepository,
)

_SNAPSHOT_HISTORY_LIMIT = 30
_RECENT_INSIGHTS_LIMIT = 10
_RECENT_ACTIVITY_LIMIT = 10


class _StatusedJob(Protocol):
    status: str
    created_at: datetime


@dataclass(frozen=True)
class IntelligenceSummary:
    files_analyzed: int
    entities_by_type: dict[str, int]
    lifecycle_by_state: dict[str, int]
    organize_suggestions: int


@dataclass(frozen=True)
class DashboardOverview:
    connectors: list[StorageConnector]
    latest_snapshot: DashboardSnapshot | None
    # Chronological (oldest first) — a trend chart reads left-to-right.
    snapshot_history: list[DashboardSnapshot]
    recent_insights: list[InsightRecord]
    recent_activity: list[File]
    latest_scan_status: str | None
    latest_enrichment_status: str | None
    latest_embedding_status: str | None
    latest_recommendation_status: str | None
    intelligence: IntelligenceSummary


class DashboardService:
    """Read-only assembly for the Founder Command Center (Phase 7 spec) —
    like `FileService`, this never computes anything itself; it just reads
    what the Scanner/Knowledge Engine/AI Intelligence Engine/Recommendation
    Engine already wrote. All queries are organization-scoped."""

    def __init__(self, db: Session) -> None:
        self._connectors = StorageConnectorRepository(db)
        self._files = FileRepository(db)
        self._scan_jobs = ScanJobRepository(db)
        self._enrichment_jobs = EnrichmentJobRepository(db)
        self._embedding_jobs = EmbeddingJobRepository(db)
        self._recommendation_jobs = RecommendationJobRepository(db)
        self._insights = InsightRecordRepository(db)
        self._snapshots = DashboardSnapshotRepository(db)
        self._intelligence = FileIntelligenceRepository(db)
        self._lifecycles = FileLifecycleRepository(db)
        self._entities = OrganizationEntityRepository(db)
        self._organization_recommendations = OrganizationRecommendationRepository(db)

    def get_overview(self, organization_id: uuid.UUID) -> DashboardOverview:
        connectors = self._connectors.list_for_organization(organization_id)

        history = list(
            reversed(
                self._snapshots.list_recent_for_organization(
                    organization_id, limit=_SNAPSHOT_HISTORY_LIMIT
                )
            )
        )
        latest_snapshot = history[-1] if history else None

        return DashboardOverview(
            connectors=connectors,
            latest_snapshot=latest_snapshot,
            snapshot_history=history,
            recent_insights=self._insights.list_recent_for_organization(
                organization_id, limit=_RECENT_INSIGHTS_LIMIT
            ),
            recent_activity=self._files.list_recently_modified_for_organization(
                organization_id, limit=_RECENT_ACTIVITY_LIMIT
            ),
            latest_scan_status=self._latest_status(
                [self._scan_jobs.list_for_connector(c.id) for c in connectors]
            ),
            latest_enrichment_status=self._latest_status(
                [self._enrichment_jobs.list_for_connector(c.id) for c in connectors]
            ),
            latest_embedding_status=self._latest_status(
                [self._embedding_jobs.list_for_connector(c.id) for c in connectors]
            ),
            latest_recommendation_status=(
                job.status
                if (job := self._recommendation_jobs.get_latest_for_organization(organization_id))
                else None
            ),
            intelligence=IntelligenceSummary(
                files_analyzed=self._intelligence.count_analyzed_for_organization(organization_id),
                entities_by_type=self._entities.count_active_by_type(organization_id),
                lifecycle_by_state=self._lifecycles.count_states_for_organization(organization_id),
                organize_suggestions=(
                    self._organization_recommendations.count_active_for_organization(
                        organization_id
                    )
                ),
            ),
        )

    @staticmethod
    def _latest_status(jobs_by_connector: list[list[_StatusedJob]]) -> str | None:
        """Each `list_for_connector` call is already ordered newest-first
        (every job repository's convention since Phase 4), so this just
        takes the most recent across however many connectors an
        organization has — 0 or 1 in practice today, but not assumed."""
        candidates = [jobs[0] for jobs in jobs_by_connector if jobs]
        if not candidates:
            return None
        latest = max(candidates, key=lambda job: job.created_at)
        return latest.status
