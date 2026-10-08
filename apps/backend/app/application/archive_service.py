import uuid
from collections.abc import Iterator

from sqlalchemy.orm import Session

from vault_shared import NotFoundError
from vault_shared.db.models import ArchiveJob, ArchiveJobStatus
from vault_shared.db.repositories import ArchiveJobRepository, AuditLogRepository
from vault_shared.object_storage import ObjectStorageClient


class ArchiveService:
    """The backend's read/delete path over `ArchiveJob` rows created by
    `ExecutionService._execute_archive_batch` (worker-side). Unlike that
    batch-creation path, list/detail/download/delete here never touch
    Google Drive at all — only Postgres and the object store — so this
    deliberately does NOT go through the Execution Engine (whose charter is
    "the only module permitted to perform a mutating call against
    *connected storage*", i.e. Drive, not the archive object store)."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._archive_jobs = ArchiveJobRepository(db)
        self._audit_logs = AuditLogRepository(db)
        self._object_storage = ObjectStorageClient()

    def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> tuple[list[ArchiveJob], int]:
        return self._archive_jobs.list_for_organization(
            organization_id, limit=limit, offset=offset
        )

    def list_archived_file_ids(self, organization_id: uuid.UUID) -> set[uuid.UUID]:
        """Backs the Trash page's per-row "already backed up?" indicator
        and the permanent-delete eligibility gate it reflects (see
        `ExecutionPlanService.create_permanent_delete_plan`)."""
        return self._archive_jobs.list_archived_file_ids(organization_id)

    def get_owned(self, archive_job_id: uuid.UUID, *, organization_id: uuid.UUID) -> ArchiveJob:
        archive_job = self._archive_jobs.get_owned(archive_job_id, organization_id=organization_id)
        if archive_job is None:
            raise NotFoundError("Archive not found.")
        return archive_job

    def get_download_stream(
        self, archive_job_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> tuple[ArchiveJob, Iterator[bytes]]:
        archive_job = self.get_owned(archive_job_id, organization_id=organization_id)
        if archive_job.status != ArchiveJobStatus.COMPLETED or not archive_job.object_storage_key:
            raise NotFoundError("This archive isn't ready to download.")
        return archive_job, self._object_storage.get_object_stream(
            key=archive_job.object_storage_key
        )

    def delete(
        self, archive_job_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        archive_job = self.get_owned(archive_job_id, organization_id=organization_id)
        if archive_job.object_storage_key:
            self._object_storage.delete_object(key=archive_job.object_storage_key)
        self._archive_jobs.mark_deleted(archive_job)
        self._audit_logs.record(
            event_type="archive_deleted",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"archive_job_id": str(archive_job.id)},
        )
        self._db.commit()
