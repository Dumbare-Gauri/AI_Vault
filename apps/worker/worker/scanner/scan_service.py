import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, ReauthRequiredError, get_logger
from vault_shared.db.models import DriveType, Folder, ScanType, StorageConnector, StorageSource
from vault_shared.db.models.scan_job import ScanJob
from vault_shared.db.repositories import (
    FileRepository,
    FolderRepository,
    ScanEventRepository,
    ScanJobRepository,
    ScanProgressRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.storage import (
    ContainerKind,
    StorageAdapter,
    StorageAdapterProvider,
    StorageFile,
)

logger = get_logger("worker.scanner.scan_service")

# The scanner speaks the provider-neutral container kinds; the inventory
# schema keeps its existing `DriveType` values, so this is the one mapping.
_DRIVE_TYPE_BY_KIND = {
    ContainerKind.PERSONAL: DriveType.MY_DRIVE,
    ContainerKind.SHARED: DriveType.SHARED_DRIVE,
}

# Bounds how much ingest work happens between commits/cancellation checks —
# keeps memory flat on very large drives and caps how much a crash mid-scan
# can lose, without paying a commit's cost on every single item.
_BATCH_COMMIT_SIZE = 200

# `folders.path`/`files.path` is String(4096) — a deeply nested tree of
# long-named folders could in principle still exceed that even with each
# individual name already bounded (by the storage adapter at the provider
# boundary),
# so the resolved path is capped defensively too rather than trusting the
# math to always stay under the column limit.
_MAX_PATH_LENGTH = 4096


class ScanCancelled(Exception):
    """Internal control-flow signal — unwinds `run()` out of whatever loop
    it's in once cooperative cancellation has been observed, without
    treating cancellation as a failure."""


class ScannerService:
    """Traverses every `StorageSource` a connector exposes and upserts
    provider-neutral `Folder`/`File` inventory rows (Handbook §8.2, §18),
    invoked from the worker's job queue (Handbook §8.15). Read-only against
    the provider: never downloads file content, never writes back to Drive.

    Ingest is two-pass per source (see ADR-016). Pass 1 streams pages from
    the Drive API and upserts each item immediately with a placeholder path
    and no resolved `parent_folder_id` — Drive's `files.list` gives no
    ordering guarantee, so a folder can be returned after its own children,
    making correct materialized paths impossible to compute in a single
    streaming pass. Pass 2 (`_resolve_hierarchy`) re-reads just that
    source's folders from our own database — a small subset of all items
    even at scale — and computes real paths via a memoized walk of each
    folder's provider-parent chain, then applies the same paths to files.
    """

    def __init__(self, db: Session, *, storage: StorageAdapterProvider) -> None:
        self._db = db
        self._storage = storage
        self._connectors = StorageConnectorRepository(db)
        self._sources = StorageSourceRepository(db)
        self._folders = FolderRepository(db)
        self._files = FileRepository(db)
        self._jobs = ScanJobRepository(db)
        self._progress = ScanProgressRepository(db)
        self._events = ScanEventRepository(db)

    def run(self, scan_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(scan_job_id)
        if job is None:
            logger.warning("scan_job_not_found", extra={"scan_job_id": str(scan_job_id)})
            return

        connector = self._connectors.get_by_id(job.connector_id)
        if connector is None:
            self._jobs.mark_failed(job, error="Connector no longer exists.")
            self._db.commit()
            return

        self._jobs.mark_running(job)
        # Idempotent: a retried task (see worker.tasks.scan) re-enters `run`
        # for the same scan_job_id, and `scan_progress.scan_job_id` is a
        # primary key, so re-creating it would raise an IntegrityError.
        if self._progress.get_for_job(job.id) is None:
            self._progress.create_for_job(job.id)
        self._events.record(scan_job_id=job.id, event_type="scan_started")
        self._db.commit()

        try:
            self._check_cancelled(job.id)
            adapter = self._storage.adapter_for(connector)
            sources = self._discover_sources(connector, adapter)
            self._progress.set_sources_discovered(job.id, len(sources))
            self._db.commit()

            for source in sources:
                self._check_cancelled(job.id)
                self._progress.set_current_source(job.id, source.name)
                self._db.commit()

                self._scan_source(job, source, adapter)

                self._progress.increment_source_completed(job.id)
                self._db.commit()
        except ScanCancelled:
            self._jobs.mark_cancelled(job)
            self._events.record(scan_job_id=job.id, event_type="scan_cancelled")
            self._db.commit()
            return
        except DependencyUnavailableError:
            # Left RUNNING, not FAILED: `worker.tasks.scan.run_scan` retries
            # the whole job on this specific error, and a misleading
            # terminal status shouldn't appear while a retry is still
            # pending. Only once retries are exhausted does the task itself
            # mark the job FAILED.
            logger.warning("scan_job_dependency_unavailable", extra={"scan_job_id": str(job.id)})
            self._db.rollback()
            raise
        except ReauthRequiredError as exc:
            # Permanent, not transient: Google has definitively rejected the
            # stored refresh token (invalid_grant). Retrying — at any layer
            # — can never succeed, so unlike DependencyUnavailableError this
            # must not be left RUNNING for a Celery retry. The connector
            # itself (not just this job) is flagged REAUTH_REQUIRED so the
            # Storage Connections page reflects it immediately, without
            # requiring the user to notice repeated scan failures first.
            logger.warning("scan_job_reauth_required", extra={"scan_job_id": str(job.id)})
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._connectors.mark_reauth_required(connector, error=str(exc))
            self._events.record(scan_job_id=job.id, event_type="scan_failed", message=str(exc))
            self._db.commit()
            return
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception("scan_job_failed", extra={"scan_job_id": str(job.id)})
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._events.record(scan_job_id=job.id, event_type="scan_failed", message=str(exc))
            self._db.commit()
            return

        connector.last_synced_at = datetime.now(UTC)
        self._jobs.mark_completed(job)
        self._events.record(scan_job_id=job.id, event_type="scan_completed")
        self._db.commit()

    def _discover_sources(
        self, connector: StorageConnector, adapter: StorageAdapter
    ) -> list[StorageSource]:
        sources = [
            self._sources.upsert(
                connector_id=connector.id,
                provider_drive_id=container.id,
                name=container.name,
                drive_type=_DRIVE_TYPE_BY_KIND[container.kind],
            )
            for container in adapter.list_containers()
        ]
        self._db.commit()
        return sources

    def _scan_source(self, job: ScanJob, source: StorageSource, adapter: StorageAdapter) -> None:
        if source.change_token is None or job.scan_type == ScanType.FULL:
            self._ingest_full(job, source, adapter)
        else:
            self._ingest_incremental(job, source, adapter)

    def _ingest_full(self, job: ScanJob, source: StorageSource, adapter: StorageAdapter) -> None:
        page_cursor: str | None = None
        pending = pending_folders = pending_files = 0

        while True:
            self._check_cancelled(job.id)
            page = adapter.scan(source.provider_drive_id, page_cursor)

            for item in page.files:
                self._ingest_item(source, item)
                pending += 1
                pending_folders += 1 if item.is_folder else 0
                pending_files += 0 if item.is_folder else 1
                if pending >= _BATCH_COMMIT_SIZE:
                    self._progress.increment_counts(
                        job.id, folders=pending_folders, files=pending_files
                    )
                    self._db.commit()
                    pending = pending_folders = pending_files = 0

            page_cursor = page.next_cursor
            if not page_cursor:
                break

        if pending:
            self._progress.increment_counts(job.id, folders=pending_folders, files=pending_files)
        self._db.commit()

        self._resolve_hierarchy(source)

        change_cursor = adapter.get_change_cursor(source.provider_drive_id)
        self._sources.update_change_token(source, change_token=change_cursor)
        self._db.commit()

    def _ingest_incremental(
        self, job: ScanJob, source: StorageSource, adapter: StorageAdapter
    ) -> None:
        change_cursor: str | None = source.change_token
        new_start_cursor: str | None = None
        pending = pending_folders = pending_files = 0
        touched = False

        while change_cursor:
            self._check_cancelled(job.id)
            page = adapter.check_changes(source.provider_drive_id, change_cursor)

            for item in page.changed:
                if item.trashed:
                    self._remove_item(source, item.provider_file_id)
                else:
                    self._ingest_item(source, item)
                    pending_folders += 1 if item.is_folder else 0
                    pending_files += 0 if item.is_folder else 1
                touched = True
                pending += 1
                if pending >= _BATCH_COMMIT_SIZE:
                    self._progress.increment_counts(
                        job.id, folders=pending_folders, files=pending_files
                    )
                    self._db.commit()
                    pending = pending_folders = pending_files = 0

            for removed_id in page.removed_ids:
                self._remove_item(source, removed_id)
                touched = True

            if page.new_start_cursor:
                new_start_cursor = page.new_start_cursor
            change_cursor = page.next_cursor

        if pending:
            self._progress.increment_counts(job.id, folders=pending_folders, files=pending_files)
        self._db.commit()

        if touched:
            self._resolve_hierarchy(source)

        if new_start_cursor:
            self._sources.update_change_token(source, change_token=new_start_cursor)
            self._db.commit()

    def _ingest_item(self, source: StorageSource, item: StorageFile) -> None:
        now = datetime.now(UTC)
        provider_parent_id = item.parent_id
        if item.is_folder:
            self._folders.upsert(
                storage_source_id=source.id,
                provider_file_id=item.provider_file_id,
                provider_parent_id=provider_parent_id,
                parent_folder_id=None,
                name=item.name,
                path=f"/{item.name}",
                owner_email=item.owner_email,
                is_shared=item.shared,
                provider_created_at=item.created_at,
                provider_modified_at=item.modified_at,
                scanned_at=now,
            )
        else:
            self._files.upsert(
                storage_source_id=source.id,
                provider_file_id=item.provider_file_id,
                provider_parent_id=provider_parent_id,
                parent_folder_id=None,
                name=item.name,
                path=f"/{item.name}",
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                owner_email=item.owner_email,
                is_shared=item.shared,
                permissions_summary="shared" if item.shared else None,
                version_id=item.revision.token,
                checksum=item.checksum,
                web_view_link=item.web_view_link,
                provider_created_at=item.created_at,
                provider_modified_at=item.modified_at,
                provider_viewed_at=item.accessed_at,
                scanned_at=now,
            )

    def _remove_item(self, source: StorageSource, provider_file_id: str) -> None:
        # The Changes API reports only a fileId for a removal, not whether it
        # was a folder or a file — try both; the (source, provider_file_id)
        # unique constraint guarantees at most one ever matches.
        self._folders.delete_by_source_and_provider_id(
            storage_source_id=source.id, provider_file_id=provider_file_id
        )
        self._files.delete_by_source_and_provider_id(
            storage_source_id=source.id, provider_file_id=provider_file_id
        )

    def _resolve_hierarchy(self, source: StorageSource) -> None:
        folders = self._folders.list_for_source(source.id)
        by_provider_id = {folder.provider_file_id: folder for folder in folders}
        resolved_paths: dict[str, str] = {}
        visiting: set[str] = set()

        def resolve_path(folder: Folder) -> str:
            if folder.provider_file_id in resolved_paths:
                return resolved_paths[folder.provider_file_id]
            if folder.provider_file_id in visiting:
                # A parent cycle should never happen in Drive, but an
                # external API's data is never trusted blindly — fall back
                # to treating this folder as its own root rather than
                # recursing forever.
                return f"/{folder.name}"

            visiting.add(folder.provider_file_id)
            parent = (
                by_provider_id.get(folder.provider_parent_id)
                if folder.provider_parent_id
                else None
            )
            path = f"{resolve_path(parent)}/{folder.name}" if parent else f"/{folder.name}"
            path = path[:_MAX_PATH_LENGTH]
            visiting.discard(folder.provider_file_id)

            resolved_paths[folder.provider_file_id] = path
            return path

        for folder in folders:
            parent = (
                by_provider_id.get(folder.provider_parent_id)
                if folder.provider_parent_id
                else None
            )
            folder.path = resolve_path(folder)
            folder.parent_folder_id = parent.id if parent else None
        self._db.commit()

        for file in self._files.list_for_source(source.id):
            parent = (
                by_provider_id.get(file.provider_parent_id) if file.provider_parent_id else None
            )
            if parent is not None:
                parent_path = resolved_paths.get(parent.provider_file_id, parent.path)
                file.path = f"{parent_path}/{file.name}"[:_MAX_PATH_LENGTH]
                file.parent_folder_id = parent.id
            else:
                file.path = f"/{file.name}"[:_MAX_PATH_LENGTH]
                file.parent_folder_id = None
        self._db.commit()

    def _check_cancelled(self, scan_job_id: uuid.UUID) -> None:
        if self._jobs.is_cancel_requested(scan_job_id):
            raise ScanCancelled()
