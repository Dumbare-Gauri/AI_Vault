import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.infrastructure.queue.storage_intelligence_producer import enqueue_storage_analysis_job
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import DuplicateGroup, File, StorageAnalysisJob, StorageAnalysisSnapshot
from vault_shared.db.repositories import (
    AuditLogRepository,
    DuplicateGroupRepository,
    FileRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
)
from vault_shared.storage_intelligence.thresholds import (
    INACTIVE_FILE_DAYS_DEFAULT,
    LARGE_FILE_BYTES_DEFAULT,
    OLD_FILE_DAYS_DEFAULT,
)


class StorageIntelligenceService:
    """Read surface for the Storage Intelligence Layer (Phase 1) plus the
    manual "re-analyze storage" trigger — mirrors `RecommendationService`'s
    read-only role exactly (the worker's `StorageIntelligenceService` does
    the actual computation; this one only reads what it wrote and enqueues
    new runs). Large/old/inactive/temporary-candidate listings are live,
    paginated, indexed queries — never persisted per-file (Phase 1 audit
    note: "don't store calculated data unnecessarily if it can safely be
    derived"); only the aggregate snapshot and duplicate groups are
    persisted, since those are genuinely expensive to (re)compute."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._files = FileRepository(db)
        self._jobs = StorageAnalysisJobRepository(db)
        self._snapshots = StorageAnalysisSnapshotRepository(db)
        self._duplicate_groups = DuplicateGroupRepository(db)
        self._audit_logs = AuditLogRepository(db)

    def get_latest_snapshot(
        self, organization_id: uuid.UUID, *, connector_id: uuid.UUID | None = None
    ) -> StorageAnalysisSnapshot | None:
        return self._snapshots.get_latest_for_organization(
            organization_id, connector_id=connector_id
        )

    def get_latest_job(self, organization_id: uuid.UUID) -> StorageAnalysisJob | None:
        return self._jobs.get_latest_for_organization(organization_id)

    def list_duplicate_groups(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[DuplicateGroup], int]:
        return self._duplicate_groups.list_for_organization(
            organization_id, limit=limit, offset=offset, connector_id=connector_id
        )

    def get_duplicate_group(
        self, group_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> tuple[DuplicateGroup, list[tuple[File, bool]]]:
        group = self._duplicate_groups.get_owned(group_id, organization_id=organization_id)
        if group is None:
            raise NotFoundError("Duplicate group not found.")
        members = [
            (file, member.is_recommended_keep)
            for member, file in self._duplicate_groups.list_members_with_files(group.id)
        ]
        return group, members

    def list_large_files(
        self,
        organization_id: uuid.UUID,
        *,
        min_size_bytes: int = LARGE_FILE_BYTES_DEFAULT,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        return self._files.list_large_for_organization(
            organization_id,
            min_size_bytes=min_size_bytes,
            limit=limit,
            offset=offset,
            connector_id=connector_id,
        )

    def list_old_files(
        self,
        organization_id: uuid.UUID,
        *,
        older_than_days: int = OLD_FILE_DAYS_DEFAULT,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        return self._files.list_old_for_organization(
            organization_id,
            older_than=cutoff,
            limit=limit,
            offset=offset,
            connector_id=connector_id,
        )

    def list_inactive_files(
        self,
        organization_id: uuid.UUID,
        *,
        inactive_days: int = INACTIVE_FILE_DAYS_DEFAULT,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        cutoff = datetime.now(UTC) - timedelta(days=inactive_days)
        return self._files.list_inactive_for_organization(
            organization_id,
            inactive_since=cutoff,
            limit=limit,
            offset=offset,
            connector_id=connector_id,
        )

    def list_candidates(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        return self._files.list_temporary_candidates_for_organization(
            organization_id, limit=limit, offset=offset, connector_id=connector_id
        )

    def trigger_analysis(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> StorageAnalysisJob:
        if self._jobs.has_active_job(organization_id):
            raise ConflictError(
                "A storage analysis run is already pending or running for this organization."
            )

        job = self._jobs.create(
            organization_id=organization_id,
            triggered_by="manual",
            triggered_by_user_id=user_id,
        )
        self._audit_logs.record(
            event_type="storage_analysis_triggered",
            organization_id=organization_id,
            user_id=user_id,
        )
        self._db.commit()

        enqueue_storage_analysis_job(job.id)
        return job
