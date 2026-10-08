import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import StorageAnalysisSnapshot


class StorageAnalysisSnapshotRepository:
    """Append-only — one row per `StorageAnalysisJob` run, mirroring
    `DashboardSnapshotRepository` exactly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        storage_analysis_job_id: uuid.UUID | None,
        total_size_bytes: int,
        total_files: int,
        total_folders: int,
        breakdown_by_type_bytes: dict,
        breakdown_by_size_bucket_bytes: dict,
        breakdown_by_source_bytes: dict,
        duplicate_group_count: int,
        duplicate_file_count: int,
        duplicate_recoverable_bytes: int,
        large_file_count: int,
        large_file_bytes: int,
        old_file_count: int,
        old_file_bytes: int,
        inactive_file_count: int,
        inactive_file_bytes: int,
        temporary_candidate_count: int,
        temporary_candidate_bytes: int,
        total_potential_savings_bytes: int,
        connector_id: uuid.UUID | None = None,
    ) -> StorageAnalysisSnapshot:
        snapshot = StorageAnalysisSnapshot(
            organization_id=organization_id,
            connector_id=connector_id,
            storage_analysis_job_id=storage_analysis_job_id,
            total_size_bytes=total_size_bytes,
            total_files=total_files,
            total_folders=total_folders,
            breakdown_by_type_bytes=breakdown_by_type_bytes,
            breakdown_by_size_bucket_bytes=breakdown_by_size_bucket_bytes,
            breakdown_by_source_bytes=breakdown_by_source_bytes,
            duplicate_group_count=duplicate_group_count,
            duplicate_file_count=duplicate_file_count,
            duplicate_recoverable_bytes=duplicate_recoverable_bytes,
            large_file_count=large_file_count,
            large_file_bytes=large_file_bytes,
            old_file_count=old_file_count,
            old_file_bytes=old_file_bytes,
            inactive_file_count=inactive_file_count,
            inactive_file_bytes=inactive_file_bytes,
            temporary_candidate_count=temporary_candidate_count,
            temporary_candidate_bytes=temporary_candidate_bytes,
            total_potential_savings_bytes=total_potential_savings_bytes,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get_latest_for_organization(
        self, organization_id: uuid.UUID, *, connector_id: uuid.UUID | None = None
    ) -> StorageAnalysisSnapshot | None:
        """The whole organization's latest snapshot, or — with `connector_id`
        — that storage's own."""
        return (
            self._session.query(StorageAnalysisSnapshot)
            .filter_by(organization_id=organization_id, connector_id=connector_id)
            .order_by(StorageAnalysisSnapshot.created_at.desc())
            .first()
        )

    def list_recent_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 30
    ) -> list[StorageAnalysisSnapshot]:
        return (
            self._session.query(StorageAnalysisSnapshot)
            .filter_by(organization_id=organization_id, connector_id=None)
            .order_by(StorageAnalysisSnapshot.created_at.desc())
            .limit(limit)
            .all()
        )
