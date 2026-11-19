import enum
import errno
import hashlib
import json
import shutil
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import IO

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from vault_shared import ConflictError, NotFoundError, ValidationError, get_logger, get_settings
from vault_shared.archive_naming import numbered_archive_name
from vault_shared.db.models import (
    ArchiveJobStatus,
    ConnectorProvider,
    ConnectorStatus,
    ExecutionActionType,
    ExecutionJob,
    ExecutionJobStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
    ExecutionResultStatus,
    ExecutionStep,
    ExecutionStepStatus,
    File,
    Folder,
    RecommendationTrigger,
    RollbackRecord,
    StorageAnalysisTrigger,
    StorageConnector,
    StorageSource,
    VerificationStatus,
)
from vault_shared.db.repositories import (
    ArchiveJobRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
    FolderRepository,
    RecommendationJobRepository,
    RollbackRecordRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.formatting import human_bytes
from vault_shared.object_storage import ObjectStorageClient
from vault_shared.protection import AssetProtection, guards
from vault_shared.storage import (
    ExportPurpose,
    ProviderFileId,
    Revision,
    StorageAdapter,
    StorageAdapterProvider,
    StorageFile,
    StorageStaleRevisionError,
    StorageUnsupportedError,
)
from vault_shared.storage.native_archive import NativeArchiver, NativeExtraction
from worker.tasks.recommendation import run_recommendation
from worker.tasks.storage_intelligence import run_storage_intelligence

logger = get_logger("worker.execution.execution_service")

_TRASHABLE_ACTIONS = (ExecutionActionType.ARCHIVE, ExecutionActionType.REMOVE_DUPLICATE)
_MOVE_ACTIONS = (ExecutionActionType.MOVE_FILE, ExecutionActionType.MOVE_FOLDER)
# PENDING covers a fresh or resumed job; RUNNING is a job whose worker died
# mid-run (Celery redelivers under `acks_late`) — the plan lock, not the
# status, is what tells a crashed run from a concurrent one.
_RUNNABLE_JOB_STATUSES = (ExecutionJobStatus.PENDING, ExecutionJobStatus.RUNNING)


_ARCHIVE_CHUNK_BYTES = 1024 * 1024
# ~60s in total — Google usually finishes emptying a large Trash within it.
# The first check is immediate: storage that deletes synchronously (this
# computer) is confirmed at once.
_EMPTY_TRASH_RECHECK_SECONDS = (0, 2, 3, 5, 8, 12, 15, 15)
_NOT_DELETED_REPORT_LIMIT = 50
_MAX_LISTED_ENTRIES = 5000
_TRASH_PREVIEW_LIMIT = 15


@dataclass(frozen=True)
class TrashItem:
    name: str
    size_bytes: int
    backed_up: bool


@dataclass(frozen=True)
class TrashSummary:
    file_count: int
    total_bytes: int
    not_backed_up_count: int
    largest: list[TrashItem]
    # Files the provider accepted for deletion but had not finished removing
    # when AI Vault last checked.
    still_deleting_count: int = 0
    # Those files by name, so the user sees exactly which ones remain.
    not_deleted: list[TrashItem] = field(default_factory=list)


_CREATABLE_MIME_TYPES = frozenset({"text/plain", "text/markdown"})
_MAX_CREATED_FILE_BYTES = 1024 * 1024
_MAX_CACHED_ARCHIVE_BYTES = 200 * 1024 * 1024


def _iter_chunks(stream: IO[bytes]) -> Iterator[bytes]:
    while chunk := stream.read(_ARCHIVE_CHUNK_BYTES):
        yield chunk


# Formats that are already compressed: deflating them again costs time for
# no space, so they are stored as-is inside the archive.
_ALREADY_COMPRESSED_EXTENSIONS = frozenset(
    {
        "zip", "rar", "7z", "gz", "tgz", "bz2", "xz",
        "mp4", "mov", "mkv", "avi", "webm", "m4v",
        "mp3", "aac", "m4a", "ogg", "flac",
        "jpg", "jpeg", "png", "gif", "webp", "heic",
        "docx", "xlsx", "pptx", "pdf",
    }
)  # fmt: skip


def _is_already_compressed(name: str) -> bool:
    return (
        name.rsplit(".", 1)[-1].lower() in _ALREADY_COMPRESSED_EXTENSIONS if "." in name else False
    )


def _archive_temp_dir() -> str:
    return get_settings().archive_temp_dir or tempfile.gettempdir()


def _require_free_space(needed: int, *, what: str) -> None:
    """Refuses up front — with the numbers — when the temporary disk can't
    hold `needed` more bytes and still keep its reserve free."""
    free = shutil.disk_usage(_archive_temp_dir()).free
    available = max(free - get_settings().archive_free_space_reserve_bytes, 0)
    if needed > available:
        raise ValidationError(
            f"Not enough free disk space to {what}: it needs about {human_bytes(needed)} "
            f"and {human_bytes(available)} is available. Free up space or archive fewer "
            "files at a time."
        )


def _write_zip_entry(
    zip_file: zipfile.ZipFile,
    name: str,
    stream: Iterable[bytes],
    *,
    store_only: bool,
) -> tuple[int, str]:
    """Streams one file into the archive chunk by chunk (ZIP64, so no 2 or
    4 GB ceiling), never holding it in memory whole. Returns
    `(bytes_written, sha256)`."""
    info = zipfile.ZipInfo(name, date_time=datetime.now(UTC).timetuple()[:6])
    info.compress_type = zipfile.ZIP_STORED if store_only else zipfile.ZIP_DEFLATED
    sha256 = hashlib.sha256()
    total = 0
    try:
        with zip_file.open(info, "w", force_zip64=True) as entry:
            for chunk in stream:
                total += len(chunk)
                sha256.update(chunk)
                entry.write(chunk)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise ValidationError(
                "The temporary disk ran out of space while building the archive — "
                "originals were left untouched. Free up space or archive fewer files."
            ) from exc
        raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return total, sha256.hexdigest()


def _verify_zip(buffer: IO[bytes], expected_entries: int) -> None:
    """Reads the finished archive back — central directory and every
    entry's CRC — before anything is uploaded or reported."""
    buffer.seek(0)
    try:
        with zipfile.ZipFile(buffer) as check:
            broken = check.testzip()
            count = len(check.infolist())
    except zipfile.BadZipFile as exc:
        raise ValidationError(
            "The archive could not be read back — originals were left untouched."
        ) from exc
    if broken is not None or count != expected_entries:
        raise ValidationError(
            f"The archive failed its integrity check ({broken or 'missing entries'}) — "
            "originals were left untouched."
        )


def _digests(stream: IO[bytes]) -> tuple[str, str]:
    """(md5, sha256) of the whole stream. MD5 is what Drive reports back for
    an uploaded file, so it is the one compared against the provider."""
    md5 = hashlib.md5()  # noqa: S324 - integrity comparison with the provider, not security
    sha256 = hashlib.sha256()
    stream.seek(0)
    for chunk in _iter_chunks(stream):
        md5.update(chunk)
        sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


@dataclass(frozen=True)
class _BuiltArchive:
    """A verified archive in the user's storage and what went into it."""

    included: list[tuple[ExecutionStep, File]]
    manifest: list[dict]
    stored: StorageFile
    original_size: int
    compressed_size: int
    md5: str | None
    sha256: str | None
    # Whether the bytes passed through this worker (and can be cached for
    # in-app download); an archive built on the user's own disk never does.
    cacheable: bool


@dataclass(frozen=True)
class ExtractionSummary:
    """The new folder an archive was unpacked into, how many files are now
    in it, and the entries left out (`{name, reason}`)."""

    folder: Folder
    file_count: int
    skipped: list[dict]


def _safe_entry_name(name: str) -> str | None:
    """An archive entry's name, if it stays inside the folder it is
    unpacked into: relative, no `.`/`..` parts, no drive letters."""
    if not name or name.startswith(("/", "\\")):
        return None
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ":" in parts[0] or ".." in parts:
        return None
    if any(any(ord(ch) < 32 for ch in part) for part in parts):
        return None
    return "/".join(parts)


def _archive_name_for(step: ExecutionStep, file: File) -> str:
    """Where a file goes inside the archive: its path below the folder the
    user archived (`archive_path`, set when a folder was chosen), so nested
    folders and identically named files keep their places; else its name."""
    return str(step.planned_change.get("archive_path") or file.name)


def _discard_unverified(adapter: StorageAdapter, stored: StorageFile) -> None:
    """An archive that failed its check is moved to Trash, never kept as if
    it were sound. Best effort: the failure itself is what gets reported."""
    try:
        adapter.trash(stored.provider_file_id)
    except Exception:  # noqa: BLE001 - cleanup must not mask the real failure
        logger.exception("archive_cleanup_failed")


def plan_lock_key(plan_id: uuid.UUID) -> int:
    """The bigint Postgres advisory-lock key serializing one plan's runs."""
    return int.from_bytes(plan_id.bytes[:8], "big", signed=True)


class _StepProgress(enum.StrEnum):
    """Where a step's target stands relative to the pre-state captured
    before the first attempt and the state the step would produce."""

    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    CONFLICT = "conflict"


class ExecutionCancelled(Exception):
    """Unwinds `run()` once cooperative cancellation has been observed —
    mirrors `ScanCancelled`/`EnrichmentCancelled`/`EmbeddingCancelled`."""


class ExecutionPaused(Exception):
    """Unwinds `run()` once cooperative pause has been observed. Unlike
    cancellation, the job is left in a resumable state — `run()`'s
    top-level handler for this is a no-op, since the pausing branch
    already committed the `PAUSED` status before raising."""


class ExecutionService:
    """The Execution Engine (Handbook §8.7) — the *only* module in this
    codebase permitted to perform a mutating call against connected
    storage — the only caller of the `StorageAdapter` methods listed in
    `MUTATING_METHODS`. It reaches storage only through the adapter for the
    plan's connection (`storage.adapter_for(connector)`), so it is
    provider-independent; every organization, retry, conflict and lock
    guarantee below applies to any provider. `run()` branches on
    `ExecutionJob.is_rollback` to either
    carry out an approved plan's steps forward or reverse a plan's
    already-executed steps via their `RollbackRecord`s — both paths share
    one job lifecycle, cooperative cancel/pause, per-step failure
    isolation, and audit trail. See ADR-020."""

    def __init__(
        self,
        db: Session,
        *,
        storage: StorageAdapterProvider,
        object_storage_client: ObjectStorageClient,
    ) -> None:
        self._db = db
        self._storage = storage
        self._object_storage = object_storage_client
        self._connectors = StorageConnectorRepository(db)
        self._plans = ExecutionPlanRepository(db)
        self._steps = ExecutionStepRepository(db)
        self._jobs = ExecutionJobRepository(db)
        self._results = ExecutionResultRepository(db)
        self._rollback_records = RollbackRecordRepository(db)
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._sources = StorageSourceRepository(db)
        self._audits = ExecutionAuditRepository(db)
        self._archive_jobs = ArchiveJobRepository(db)
        self._storage_analysis_jobs = StorageAnalysisJobRepository(db)
        self._recommendation_jobs = RecommendationJobRepository(db)
        self._protection = AssetProtection(db)

    def create_folder(
        self,
        *,
        organization_id: uuid.UUID,
        storage_source_id: uuid.UUID,
        name: str,
        parent_folder_id: uuid.UUID | None = None,
    ) -> Folder:
        """Phase 2 — the Organization Recommendation Engine's folder-
        creation step (`worker.organization.
        organization_recommendation_apply_service`). Deliberately outside
        the `ExecutionStep`/`ExecutionPlan`/`RollbackRecord` machinery: a
        folder that doesn't exist yet has no `provider_file_id` for that
        machinery's crash-safe retry/conflict detection to compare against
        (see the plan's own design notes on why `ExecutionStep.
        target_file_id` can't represent this). The adapter's successful
        return is this call's own verification — no separate step/result
        row, no rollback record.

        Crash-safety is narrower than `ExecutionStep`'s, and that's stated
        here rather than implied: a crash between the Drive call
        succeeding and the commit below could leave an orphaned empty
        folder in Drive with no local `Folder` row. That's self-healing,
        not silent data loss — the next scan's `FolderRepository.upsert`
        is keyed identically and will discover it naturally — but it is
        not the same crash-safe retry guarantee the rest of this engine
        provides, and a caller should not assume otherwise."""
        source = self._sources.get_by_id(storage_source_id)
        if source is None:
            raise NotFoundError("Storage source not found.")
        connector = self._connectors.get_by_id(source.connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Storage source not found.")

        parent_folder: Folder | None = None
        parent_provider_id: str | None = None
        parent_path = ""
        if parent_folder_id is not None:
            parent_folder = self._folders.get_by_id(parent_folder_id)
            if parent_folder is None or parent_folder.storage_source_id != storage_source_id:
                raise NotFoundError("Parent folder not found.")
            parent_provider_id = parent_folder.provider_file_id
            parent_path = parent_folder.path.rstrip("/")

        failures = self._permission_failures(connector)
        if failures:
            raise ValidationError("; ".join(failures))
        if parent_provider_id is None:
            parent_provider_id = self._top_parent_id(connector, source)

        # The one mutating call in this method — kept here, in
        # `ExecutionService`, so it is covered by the same static guard
        # (`test_adapter_mutations_are_called_only_by_the_execution_service`)
        # every other Drive mutation in this file already is.
        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        created = adapter.create_folder(
            name,
            parent_id=ProviderFileId(parent_provider_id) if parent_provider_id else None,
        )

        now = datetime.now(UTC)
        folder = self._folders.upsert(
            storage_source_id=storage_source_id,
            provider_file_id=created.provider_file_id,
            provider_parent_id=parent_provider_id,
            parent_folder_id=parent_folder.id if parent_folder else None,
            name=created.name,
            path=f"{parent_path}/{created.name}",
            owner_email=created.owner_email,
            is_shared=created.shared,
            provider_created_at=created.created_at,
            provider_modified_at=created.modified_at,
            scanned_at=now,
        )
        self._audits.record(
            organization_id=organization_id,
            event_type="folder_created",
            metadata={
                "folder_id": str(folder.id),
                "name": folder.name,
                "parent_folder_id": str(parent_folder_id) if parent_folder_id else None,
            },
        )
        self._db.commit()
        return folder

    @staticmethod
    def _top_parent_id(connector: StorageConnector, source: StorageSource) -> str | None:
        """Where a top-level item of a storage source goes. On this computer a
        source *is* an authorized folder, and the agent needs to be told
        which one; Google Drive takes "no parent" as the top of My Drive."""
        if connector.provider == ConnectorProvider.LOCAL_AGENT:
            return source.provider_drive_id
        return None

    def preview_trash(self, *, organization_id: uuid.UUID, connector_id: uuid.UUID) -> TrashSummary:
        """What emptying the provider's Trash would permanently delete — read
        only. `backed_up` marks files that already have a completed AI Vault
        archive; everything else would be gone for good."""
        connector, adapter = self._connected_adapter(organization_id, connector_id)
        trashed = adapter.list_trash()
        self._record_trash_state(connector, adapter, trash_is_empty=not trashed)
        return self._summarize_trash(organization_id, trashed)

    def empty_trash(
        self, *, organization_id: uuid.UUID, connector_id: uuid.UUID, expected_count: int
    ) -> TrashSummary:
        """Permanently deletes everything in the provider's Trash, then reads
        the Trash back from the provider to confirm it is empty. Refuses if the
        Trash changed since the user reviewed it, so it never deletes more
        than the user confirmed."""
        connector, adapter = self._connected_adapter(organization_id, connector_id)
        failures = self._permission_failures(connector)
        if failures:
            raise ValidationError("; ".join(failures))
        trashed = adapter.list_trash()
        if not trashed:
            self._record_trash_state(connector, adapter, trash_is_empty=True)
            return self._summarize_trash(organization_id, trashed)
        if len(trashed) != expected_count:
            raise ConflictError(
                f"Drive's Trash changed since you reviewed it (it now holds {len(trashed)} "
                "files). Review it again before emptying."
            )
        summary = self._summarize_trash(organization_id, trashed)

        adapter.empty_trash()

        # The provider accepts the request at once but deletes in the
        # background, so the Trash is re-read until it is empty.
        remaining: list[StorageFile] = trashed
        for delay in _EMPTY_TRASH_RECHECK_SECONDS:
            time.sleep(delay)
            remaining = adapter.list_trash()
            if not remaining:
                break
        remaining_ids = {item.provider_file_id for item in remaining}

        sources = [source.id for source in self._sources.list_for_connector(connector.id)]
        for item in trashed:
            if item.provider_file_id in remaining_ids:
                continue
            for source_id in sources:
                file = self._files.get_by_source_and_provider_id(
                    storage_source_id=source_id, provider_file_id=item.provider_file_id
                )
                if file is not None:
                    self._files.mark_permanently_deleted(file)

        deleted_count = len(trashed) - len(remaining_ids)
        self._audits.record(
            organization_id=organization_id,
            event_type="trash_emptied",
            message=f"{deleted_count} files permanently deleted from the provider's Trash.",
            metadata={
                "connector_id": str(connector.id),
                "deleted_count": deleted_count,
                "deleted_bytes": sum(
                    item.size_bytes or 0
                    for item in trashed
                    if item.provider_file_id not in remaining_ids
                ),
                "still_deleting_count": len(remaining_ids),
            },
        )
        self._db.commit()
        self._record_trash_state(connector, adapter, trash_is_empty=not remaining_ids)
        return replace(
            summary,
            still_deleting_count=len(remaining_ids),
            not_deleted=[
                TrashItem(name=item.name, size_bytes=item.size_bytes or 0, backed_up=False)
                for item in remaining[:_NOT_DELETED_REPORT_LIMIT]
            ],
        )

    def _record_trash_state(
        self, connector: StorageConnector, adapter: StorageAdapter, *, trash_is_empty: bool
    ) -> None:
        """Re-reads storage use from the provider. When the provider lists
        nothing in Trash, Trash holds nothing — even while its quota figure
        still lags behind the background deletion."""
        self._refresh_quota(connector, adapter)
        if trash_is_empty and connector.storage_trash_bytes:
            connector.storage_trash_bytes = 0
            self._db.commit()

    def _connected_adapter(
        self, organization_id: uuid.UUID, connector_id: uuid.UUID
    ) -> tuple[StorageConnector, StorageAdapter]:
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connection not found.")
        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        return connector, adapter

    def _summarize_trash(
        self, organization_id: uuid.UUID, trashed: list[StorageFile]
    ) -> TrashSummary:
        archived_ids = self._archive_jobs.list_archived_file_ids(organization_id)
        backed_up = {file.provider_file_id for file in self._files.list_by_ids(list(archived_ids))}
        items = sorted(
            (
                TrashItem(
                    name=item.name,
                    size_bytes=item.size_bytes or 0,
                    backed_up=item.provider_file_id in backed_up,
                )
                for item in trashed
            ),
            key=lambda entry: entry.size_bytes,
            reverse=True,
        )
        return TrashSummary(
            file_count=len(items),
            total_bytes=sum(entry.size_bytes for entry in items),
            not_backed_up_count=sum(1 for entry in items if not entry.backed_up),
            largest=items[:_TRASH_PREVIEW_LIMIT],
        )

    def create_text_file(
        self,
        *,
        organization_id: uuid.UUID,
        storage_source_id: uuid.UUID,
        name: str,
        content: str,
        mime_type: str,
        parent_folder_id: uuid.UUID | None = None,
    ) -> File:
        """Creates a new text file in the user's storage. Never overwrites:
        the provider always creates a new item. Verified by reading the new
        item back from the provider before it is recorded."""
        if mime_type not in _CREATABLE_MIME_TYPES:
            raise ValidationError("Only plain text and Markdown files can be created.")
        data = content.encode("utf-8")
        if len(data) > _MAX_CREATED_FILE_BYTES:
            raise ValidationError("The file is too large to create this way.")

        source = self._sources.get_by_id(storage_source_id)
        connector = self._connectors.get_by_id(source.connector_id) if source else None
        if source is None or connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Storage source not found.")
        parent_folder: Folder | None = None
        if parent_folder_id is not None:
            parent_folder = self._folders.get_by_id(parent_folder_id)
            if parent_folder is None or parent_folder.storage_source_id != storage_source_id:
                raise NotFoundError("Parent folder not found.")
        failures = self._permission_failures(connector)
        if failures:
            raise ValidationError("; ".join(failures))

        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        parent_id = (
            parent_folder.provider_file_id
            if parent_folder
            else self._top_parent_id(connector, source)
        )
        created = adapter.upload(
            name,
            iter([data]),
            mime_type=mime_type,
            parent_id=ProviderFileId(parent_id) if parent_id else None,
        )
        stored = adapter.get_file(created.provider_file_id)
        if stored.size_bytes is not None and stored.size_bytes != len(data):
            raise ValidationError("The provider stored a different file than was sent.")

        file = self._record_new_file(storage_source_id, parent_folder, stored)
        self._audits.record(
            organization_id=organization_id,
            event_type="file_created",
            metadata={"file_id": str(file.id), "name": file.name},
        )
        self._db.commit()
        return file

    def make_copy(
        self, *, organization_id: uuid.UUID, file_id: uuid.UUID, new_name: str | None = None
    ) -> File:
        """Makes a copy next to the original in the user's storage, then reads
        the copy back from the provider and checks it matches the original
        before recording it."""
        source_file = self._files.get_by_id(file_id)
        source = self._sources.get_by_id(source_file.storage_source_id) if source_file else None
        connector = self._connectors.get_by_id(source.connector_id) if source else None
        if (
            source_file is None
            or source is None
            or connector is None
            or connector.organization_id != organization_id
            or source_file.trashed
        ):
            raise NotFoundError("File not found.")
        failures = self._permission_failures(connector)
        if failures:
            raise ValidationError("; ".join(failures))

        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        copied = adapter.copy(
            ProviderFileId(source_file.provider_file_id),
            new_name=new_name or f"Copy of {source_file.name}",
            parent_id=(
                ProviderFileId(source_file.provider_parent_id)
                if source_file.provider_parent_id
                else None
            ),
        )
        stored = adapter.get_file(copied.provider_file_id)
        original = adapter.get_file(ProviderFileId(source_file.provider_file_id))
        if stored.trashed or (
            original.size_bytes is not None and stored.size_bytes != original.size_bytes
        ):
            raise ValidationError("The provider's copy does not match the original.")
        if original.checksum and stored.checksum and original.checksum != stored.checksum:
            raise ValidationError("The provider's copy does not match the original.")

        parent_folder = (
            self._folders.get_by_id(source_file.parent_folder_id)
            if source_file.parent_folder_id
            else None
        )
        file = self._record_new_file(source.id, parent_folder, stored)
        self._audits.record(
            organization_id=organization_id,
            event_type="file_copied",
            metadata={"file_id": str(file.id), "name": file.name, "source_file_id": str(file_id)},
        )
        self._db.commit()
        return file

    def _record_new_file(
        self, storage_source_id: uuid.UUID, parent_folder: Folder | None, stored: StorageFile
    ) -> File:
        now = datetime.now(UTC)
        parent_path = parent_folder.path.rstrip("/") if parent_folder else ""
        return self._files.upsert(
            storage_source_id=storage_source_id,
            provider_file_id=stored.provider_file_id,
            provider_parent_id=(
                parent_folder.provider_file_id if parent_folder else stored.parent_id
            ),
            parent_folder_id=parent_folder.id if parent_folder else None,
            name=stored.name,
            path=f"{parent_path}/{stored.name}",
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            owner_email=stored.owner_email,
            is_shared=stored.shared,
            permissions_summary=None,
            version_id=stored.revision.token,
            checksum=stored.checksum,
            web_view_link=stored.web_view_link,
            provider_created_at=stored.created_at,
            provider_modified_at=stored.modified_at,
            provider_viewed_at=stored.accessed_at,
            scanned_at=now,
        )

    def extract_archive(
        self,
        *,
        organization_id: uuid.UUID,
        file_id: uuid.UUID,
        destination_folder_id: uuid.UUID | None = None,
    ) -> "ExtractionSummary":
        """Unpacks a zip from the user's storage into a new folder (named
        after the archive) in the same storage — the archive's own folder
        unless the user picked another. Nothing is overwritten and no entry
        can land outside the new folder. Everything created is read back
        from the storage and added to the index, so Files shows it at
        once."""
        owned = self._files.get_owned_with_connector(file_id, organization_id=organization_id)
        if owned is None:
            raise NotFoundError("File not found.")
        archive, connector = owned
        if not archive.name.lower().endswith(".zip") or archive.trashed:
            raise ValidationError("Only a .zip archive that isn't in Trash can be extracted.")
        source = self._sources.get_by_id(archive.storage_source_id)
        assert source is not None
        destination = self._extraction_destination(archive, destination_folder_id)
        failures = self._permission_failures(connector)
        if failures:
            raise ValidationError("; ".join(failures))

        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        parent_id = (
            destination.provider_file_id if destination else self._top_parent_id(connector, source)
        )
        if isinstance(adapter, NativeArchiver):
            if parent_id is None:
                raise ValidationError("Choose a folder to extract into.")
            extracted = adapter.extract_archive(
                ProviderFileId(archive.provider_file_id),
                parent_id=ProviderFileId(parent_id),
                expected_bytes=archive.size_bytes or 0,
            )
            summary = self._record_extraction(source.id, destination, extracted)
        else:
            summary = self._extract_by_upload(
                adapter, archive, source_id=source.id, destination=destination, parent_id=parent_id
            )
        self._audits.record(
            organization_id=organization_id,
            event_type="archive_extracted",
            metadata={
                "connector_id": str(connector.id),
                "file_id": str(archive.id),
                "folder_id": str(summary.folder.id),
                "name": summary.folder.name,
                "file_count": summary.file_count,
                "skipped": summary.skipped[:50],
            },
        )
        self._db.commit()
        return summary

    def inspect_archive(self, *, organization_id: uuid.UUID, file_id: uuid.UUID) -> dict:
        """What a zip in the user's storage holds — read only, nothing is
        unpacked or changed. Entries that would escape their folder on
        extraction are flagged, so the user sees them before extracting."""
        owned = self._files.get_owned_with_connector(file_id, organization_id=organization_id)
        if owned is None:
            raise NotFoundError("File not found.")
        archive, connector = owned
        if not archive.name.lower().endswith(".zip") or archive.trashed:
            raise ValidationError("Only a .zip archive that isn't in Trash can be inspected.")
        adapter = self._storage.adapter_for(connector)
        adapter.connect()
        if isinstance(adapter, NativeArchiver):
            return adapter.archive_contents(
                ProviderFileId(archive.provider_file_id), expected_bytes=archive.size_bytes or 0
            )
        _require_free_space(archive.size_bytes or 0, what="read this archive")
        with tempfile.TemporaryFile(dir=_archive_temp_dir()) as spool:
            for chunk in adapter.open_read(ProviderFileId(archive.provider_file_id)):
                spool.write(chunk)
            spool.seek(0)
            try:
                with zipfile.ZipFile(spool) as bundle:
                    members = bundle.infolist()
            except zipfile.BadZipFile as exc:
                raise ValidationError("That file isn't a readable zip archive.") from exc
        entries = [
            {
                "name": member.filename,
                "size_bytes": member.file_size,
                "compressed_bytes": member.compress_size,
                "is_folder": member.is_dir(),
                "safe": _safe_entry_name(member.filename.rstrip("/")) is not None,
            }
            for member in members
        ]
        return {
            "entries": entries[:_MAX_LISTED_ENTRIES],
            "entry_count": len(entries),
            "file_count": sum(1 for entry in entries if not entry["is_folder"]),
            "total_bytes": sum(entry["size_bytes"] for entry in entries),
            "unsafe_count": sum(1 for entry in entries if not entry["safe"]),
        }

    def _extraction_destination(
        self, archive: File, destination_folder_id: uuid.UUID | None
    ) -> Folder | None:
        folder_id = destination_folder_id or archive.parent_folder_id
        if folder_id is None:
            return None
        folder = self._folders.get_by_id(folder_id)
        if folder is None or folder.storage_source_id != archive.storage_source_id:
            raise NotFoundError("Choose a folder in the same storage as the archive.")
        return folder

    def _record_extraction(
        self, source_id: uuid.UUID, destination: Folder | None, extracted: "NativeExtraction"
    ) -> "ExtractionSummary":
        """Adds what the storage reported creating to the index, parents
        before children, so the new folder tree appears in Files as is."""
        root = self._record_folder(source_id, destination, extracted.folder)
        folders: dict[str, Folder] = {root.provider_file_id: root}
        file_count = 0
        for item in extracted.created:
            parent = folders.get(item.parent_id or "", root)
            if item.is_folder:
                folders[item.provider_file_id] = self._record_folder(source_id, parent, item)
            else:
                self._record_new_file(source_id, parent, item)
                file_count += 1
        return ExtractionSummary(folder=root, file_count=file_count, skipped=extracted.skipped)

    def _record_folder(
        self, source_id: uuid.UUID, parent: Folder | None, stored: StorageFile
    ) -> Folder:
        parent_path = parent.path.rstrip("/") if parent else ""
        return self._folders.upsert(
            storage_source_id=source_id,
            provider_file_id=stored.provider_file_id,
            provider_parent_id=parent.provider_file_id if parent else stored.parent_id,
            parent_folder_id=parent.id if parent else None,
            name=stored.name,
            path=f"{parent_path}/{stored.name}",
            owner_email=stored.owner_email,
            is_shared=stored.shared,
            provider_created_at=stored.created_at,
            provider_modified_at=stored.modified_at,
            scanned_at=datetime.now(UTC),
        )

    def _extract_by_upload(
        self,
        adapter: StorageAdapter,
        archive: File,
        *,
        source_id: uuid.UUID,
        destination: Folder | None,
        parent_id: str | None,
    ) -> "ExtractionSummary":
        """For storage that can't unpack an archive itself (Google Drive):
        the zip is read into a temporary file here, every entry name is
        checked, and each folder and file is created in the storage and read
        back before it is recorded. Never overwrites: everything goes into a
        new folder."""
        _require_free_space(archive.size_bytes or 0, what="extract this archive")
        stem = archive.name[:-4] if archive.name.lower().endswith(".zip") else archive.name
        skipped: list[dict] = []
        file_count = 0
        with tempfile.TemporaryFile(dir=_archive_temp_dir()) as spool:
            for chunk in adapter.open_read(ProviderFileId(archive.provider_file_id)):
                spool.write(chunk)
            spool.seek(0)
            try:
                bundle = zipfile.ZipFile(spool)
            except zipfile.BadZipFile as exc:
                raise ValidationError("That file isn't a readable zip archive.") from exc
            with bundle:
                created_root = adapter.create_folder(
                    stem, parent_id=ProviderFileId(parent_id) if parent_id else None
                )
                root = self._record_folder(source_id, destination, created_root)
                folders: dict[str, Folder] = {"": root}
                for member in bundle.infolist():
                    clean = _safe_entry_name(member.filename.rstrip("/"))
                    if clean is None:
                        skipped.append({"name": member.filename, "reason": "Unsafe path"})
                        continue
                    parts = clean.split("/")
                    dirs = parts if member.is_dir() else parts[:-1]
                    parent = root
                    for depth in range(1, len(dirs) + 1):
                        key = "/".join(dirs[:depth])
                        if key not in folders:
                            made = adapter.create_folder(
                                dirs[depth - 1], parent_id=ProviderFileId(parent.provider_file_id)
                            )
                            folders[key] = self._record_folder(source_id, parent, made)
                        parent = folders[key]
                    if member.is_dir():
                        continue
                    with bundle.open(member) as reader:
                        uploaded = adapter.upload(
                            parts[-1],
                            _iter_chunks(reader),
                            mime_type="application/octet-stream",
                            parent_id=ProviderFileId(parent.provider_file_id),
                        )
                    stored = adapter.get_file(uploaded.provider_file_id)
                    if stored.size_bytes is not None and stored.size_bytes != member.file_size:
                        skipped.append({"name": clean, "reason": "Stored size didn't match"})
                        continue
                    self._record_new_file(source_id, parent, stored)
                    file_count += 1
        return ExtractionSummary(folder=root, file_count=file_count, skipped=skipped)

    def run(self, execution_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(execution_job_id)
        if job is None:
            logger.warning(
                "execution_job_not_found", extra={"execution_job_id": str(execution_job_id)}
            )
            return

        if job.status not in _RUNNABLE_JOB_STATUSES:
            # A redelivery of a message whose job already finished (the ack
            # was lost), or a stale message for a job that has since been
            # paused or cancelled: never re-run it.
            logger.info(
                "execution_job_not_runnable",
                extra={"execution_job_id": str(job.id), "status": job.status},
            )
            return

        plan = self._plans.get_by_id(job.execution_plan_id)
        if plan is None:
            self._jobs.mark_failed(job, error="Execution plan no longer exists.")
            self._db.commit()
            return

        with self._exclusive_plan_lock(plan.id) as acquired:
            if not acquired:
                logger.warning(
                    "execution_plan_locked_by_another_worker",
                    extra={"execution_job_id": str(job.id), "execution_plan_id": str(plan.id)},
                )
                return
            # The job may have finished between the status check above and
            # taking the lock (duplicate delivery racing the first worker).
            self._db.refresh(job)
            if job.status not in _RUNNABLE_JOB_STATUSES:
                return
            self._run_job(job, plan)

    @contextmanager
    def _exclusive_plan_lock(self, plan_id: uuid.UUID) -> Iterator[bool]:
        """At most one worker executes a plan's steps at a time. A Postgres
        session-level advisory lock on a dedicated connection — not the ORM
        session's, whose connection is released at every commit — so it
        survives the engine's many commits and is released by the database
        itself if the worker process dies, which is what lets a redelivered
        crash-recovery run acquire it while a genuinely concurrent duplicate
        cannot."""
        key = plan_lock_key(plan_id)
        bind = self._db.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        connection = engine.connect()
        acquired = False
        try:
            acquired = bool(
                connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            )
            yield acquired
        finally:
            try:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            finally:
                connection.close()

    def _run_job(self, job: ExecutionJob, plan: ExecutionPlan) -> None:
        connector = self._connectors.get_by_organization_and_provider(
            organization_id=plan.organization_id, provider=plan.target_provider
        )
        failures = self._permission_failures(connector)
        if failures:
            error = "; ".join(failures)
            self._jobs.mark_failed(job, error=error)
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_failed",
                message=error,
            )
            self._db.commit()
            return

        self._jobs.mark_running(job)
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_started",
            metadata={"is_rollback": job.is_rollback},
        )
        if not job.is_rollback:
            self._plans.update_status(plan, status=ExecutionPlanStatus.EXECUTING)
        self._db.commit()

        assert connector is not None  # _permission_failures already checked this
        adapter = self._storage.adapter_for(connector)

        try:
            # Inside the try: a credential/decrypt/auth failure here must fail
            # the job, not leave it RUNNING (and its plan EXECUTING) forever.
            adapter.connect()
            if job.is_rollback:
                self._run_rollback(job, plan, adapter=adapter)
            else:
                self._run_forward(job, plan, adapter=adapter)
            self._refresh_quota(connector, adapter)
        except ExecutionCancelled:
            self._jobs.mark_cancelled(job)
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_cancelled",
            )
            self._db.commit()
        except ExecutionPaused:
            pass  # the pausing branch already committed PAUSED before raising
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception("execution_job_failed", extra={"execution_job_id": str(job.id)})
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            if not job.is_rollback:
                self._plans.update_status(plan, status=ExecutionPlanStatus.FAILED)
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_failed",
                message=str(exc),
            )
            self._db.commit()

    def _permission_failures(self, connector: StorageConnector | None) -> list[str]:
        """Defense-in-depth re-check right before executing, since time
        passes between approval and execution. Connector availability is
        application state; whether the *grant* allows mutations is a question
        for the provider's adapter (what "write access" means differs per
        provider), and no credential is read here."""
        if connector is None:
            return ["Connector no longer exists."]
        failures: list[str] = []
        if connector.status != ConnectorStatus.CONNECTED:
            failures.append(f"Connector is not connected (status: {connector.status}).")
        try:
            failures.extend(self._storage.adapter_for(connector).write_access_problems())
        except StorageUnsupportedError as exc:
            failures.append(exc.message)
        return failures

    # ------------------------------------------------------------------
    # Forward execution
    # ------------------------------------------------------------------

    def _run_forward(
        self, job: ExecutionJob, plan: ExecutionPlan, *, adapter: StorageAdapter
    ) -> None:
        steps = self._steps.list_for_plan(plan.id)

        # CREATE_ARCHIVE is the one action type that isn't "one step = one
        # independent mutation" — N files become ONE zip in ONE object
        # storage write, so all of a plan's CREATE_ARCHIVE steps are
        # completed together here, before the per-step loop below (which
        # only ever processes still-PENDING steps) ever reaches them.
        # Cancellation is checked once, before the batch starts, not
        # per-file within it — zip-building isn't resumable mid-stream.
        pending_archive_steps = [
            step
            for step in steps
            if step.action_type == ExecutionActionType.CREATE_ARCHIVE
            and step.status == ExecutionStepStatus.PENDING
        ]
        if pending_archive_steps:
            self._check_cancelled(job.id)
            self._execute_archive_batch(job, plan, pending_archive_steps, adapter=adapter)

        succeeded = 0
        failed = 0
        for step in steps:
            if step.status != ExecutionStepStatus.PENDING:
                # Resuming a previously-paused job — already-decided steps
                # are never re-executed.
                if step.status == ExecutionStepStatus.COMPLETED:
                    succeeded += 1
                elif step.status == ExecutionStepStatus.FAILED:
                    failed += 1
                continue

            self._check_cancelled(job.id)
            if self._jobs.is_pause_requested(job.id):
                self._jobs.mark_paused(job)
                self._audits.record(
                    organization_id=plan.organization_id,
                    execution_plan_id=plan.id,
                    execution_job_id=job.id,
                    event_type="execution_paused",
                )
                self._db.commit()
                raise ExecutionPaused

            if self._execute_forward_step(job, plan, step, adapter=adapter):
                succeeded += 1
            else:
                failed += 1

        if failed == 0:
            self._jobs.mark_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.COMPLETED)
        elif succeeded == 0:
            self._jobs.mark_failed(job, error="All steps failed.")
            self._plans.update_status(plan, status=ExecutionPlanStatus.FAILED)
        else:
            self._jobs.mark_partially_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.PARTIALLY_COMPLETED)

        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_completed",
            metadata={"succeeded": succeeded, "failed": failed},
        )
        self._db.commit()

        if any(
            step.action_type in _TRASHABLE_ACTIONS and step.status == ExecutionStepStatus.COMPLETED
            for step in steps
        ):
            self._trigger_storage_refresh(plan.organization_id)

    def _refresh_quota(self, connector: StorageConnector, adapter: StorageAdapter) -> None:
        """Storage use changes with every move to Trash or restore, so the
        dashboard re-reads it from the provider after each action rather than
        waiting for the next scan. Informational only — never fails a job."""
        try:
            quota = adapter.storage_quota()
        except Exception:  # noqa: BLE001 - quota is informational
            logger.warning("storage_quota_unavailable", extra={"connector_id": str(connector.id)})
            return
        connector.storage_used_bytes = quota.used_bytes
        connector.storage_total_bytes = quota.total_bytes
        connector.storage_trash_bytes = quota.trash_bytes
        connector.quota_checked_at = datetime.now(UTC)
        self._db.commit()

    def _execute_forward_step(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        step: ExecutionStep,
        *,
        adapter: StorageAdapter,
    ) -> bool:
        now = datetime.now(UTC)
        already_applied = False
        try:
            file = self._get_plan_file(plan, step)

            # `ExecutionStep.id` is this step's idempotency identity, and
            # `rollback_records.execution_step_id` is unique. A record for a
            # still-PENDING step therefore means an earlier attempt died
            # (worker crash, `acks_late` redelivery) after capturing
            # pre-state and possibly after the Drive write. That record is
            # the authoritative pre-state and must survive the retry.
            record = self._rollback_records.get_by_step(step.id)

            # Live "resource existence" + "current file state" validation
            # (Phase 8 spec) — right before mutating, since provider state
            # can have drifted since the plan was created or even since
            # approval.
            current: StorageFile | None
            try:
                current = adapter.get_file(ProviderFileId(file.provider_file_id))
            except NotFoundError:
                # Only a retry of a permanent delete may find the file gone:
                # that is the mutation's own result.
                if record is None or step.action_type != ExecutionActionType.PERMANENT_DELETE:
                    raise
                current = None

            if record is None:
                assert current is not None  # NotFound is only swallowed when a record exists
                # PERMANENT_DELETE is the one action that *requires* the
                # file already be trashed (that's its whole eligibility rule,
                # see ExecutionPlanService.create_permanent_delete_plan);
                # every other action expects the opposite.
                if step.action_type == ExecutionActionType.PERMANENT_DELETE:
                    if not current.trashed:
                        raise ConflictError(
                            "File is no longer in Trash — refusing to delete it permanently."
                        )
                elif step.action_type == ExecutionActionType.RESTORE:
                    if not current.trashed:
                        raise ConflictError("File is not in Trash — nothing to restore.")
                elif current.trashed:
                    raise ConflictError(
                        "File is already trashed outside this platform — nothing to do."
                    )
                self._reject_if_stale(step, current)
                self._reject_if_moved(step, current)
                self._reject_if_protected(step, file)
                # The authoritative rollback source — captured and committed
                # *before* the live mutation, so a failure after a
                # successful Drive write can never leave a mutation with no
                # way back.
                pre_state = self._pre_mutation_state(step.action_type, current)
                self._rollback_records.create(execution_step_id=step.id, pre_state=pre_state)
                self._db.commit()
                progress = _StepProgress.NOT_APPLIED
            else:
                progress = self._observe_progress(step, current, record.pre_state)

            if progress is _StepProgress.CONFLICT:
                raise ConflictError(
                    "The file matches neither its state before nor after this step, so it "
                    "was changed by someone else — refusing to overwrite that change."
                )
            if progress is _StepProgress.APPLIED:
                already_applied = True
                self._reconcile_local_mirror(step, file, current)
            else:
                assert current is not None
                self._apply_action(step, adapter=adapter, file=file, current=current)
        except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the job
            logger.exception("execution_step_failed", extra={"execution_step_id": str(step.id)})
            self._db.rollback()
            self._steps.mark_failed(step)
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.FAILED,
                verification_status=VerificationStatus.SKIPPED,
                error=str(exc),
                executed_at=now,
                verified_at=None,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_failed",
                message=str(exc),
                metadata={
                    "execution_step_id": str(step.id),
                    "action_type": step.action_type,
                    "error_type": type(exc).__name__,
                },
            )
            self._db.commit()
            return False

        if already_applied:
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_already_applied",
                message="An earlier attempt had already made this change; it was not repeated.",
                metadata={"execution_step_id": str(step.id), "action_type": step.action_type},
            )

        # The mutation itself succeeded — a verification problem is
        # recorded, not treated as step failure (Phase 8 spec: "Verification
        # failures should trigger recovery procedures," not undo a
        # successful action retroactively).
        try:
            verified = self._verify_forward(step, adapter=adapter, file=file)
            verification_status = (
                VerificationStatus.VERIFIED if verified else VerificationStatus.FAILED
            )
        except Exception:  # noqa: BLE001 - verification itself failing is not a step failure
            logger.exception(
                "execution_step_verification_failed", extra={"execution_step_id": str(step.id)}
            )
            verification_status = VerificationStatus.FAILED

        self._steps.mark_completed(step)
        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.SUCCESS,
            verification_status=verification_status,
            error=None,
            executed_at=now,
            verified_at=datetime.now(UTC),
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="step_completed",
            metadata={
                "execution_step_id": str(step.id),
                "action_type": step.action_type,
                "verification_status": verification_status,
            },
        )
        self._db.commit()
        return True

    def _get_plan_file(self, plan: ExecutionPlan, step: ExecutionStep) -> File:
        """Every mutation is scoped to the plan's own organization here, at
        the point of mutation. Plan creation already checks ownership, but
        the engine must not trust that a step's file id was vetted: one wrong
        id would otherwise act on another tenant's inventory. The same
        not-found error covers 'missing' and 'someone else's', so nothing
        reveals that another organization's file exists."""
        owned = self._files.get_owned_with_connector(
            step.target_file_id, organization_id=plan.organization_id
        )
        if owned is None or owned[1].provider != plan.target_provider:
            raise NotFoundError("Target file no longer exists in this platform's inventory.")
        return owned[0]

    @staticmethod
    def _reject_if_stale(step: ExecutionStep, current: StorageFile) -> None:
        """A plan reviewed against one version of a file must not be applied
        to a different one. The plan records the file's revision at creation
        (`step.pre_state["revision"]`); if the provider now reports a
        different content revision, someone edited the file since and the
        step is refused rather than applied blindly. Steps created before
        revisions were recorded, and providers that expose no content
        revision for an item, are not blocked — `Revision.differs_from` only
        declares a difference when both sides carry a token."""
        expected = Revision.from_dict(step.pre_state.get("revision"))
        if expected is not None and expected.differs_from(current.revision):
            raise StorageStaleRevisionError(
                "The file was edited after this plan was created — refusing to change a "
                "version that was never reviewed.",
                provider=current.provider,
                provider_code="stale_revision",
            )

    @staticmethod
    def _reject_if_moved(step: ExecutionStep, current: StorageFile) -> None:
        """The user confirmed a change to a file at a particular name and
        place. If the provider now reports it under another name or in
        another folder, the step is refused rather than applied to a file
        the user never saw there. Steps planned before this was recorded
        carry no expectation and are not blocked."""
        if not guards(step.action_type, step.planned_change):
            return
        expected_name = step.pre_state.get("name")
        if expected_name and current.name != expected_name:
            raise ConflictError(
                f"The file is now called “{current.name}”, not “{expected_name}” as when this "
                "was reviewed — refusing to act on it. Rescan and review it again."
            )
        expected_parent = step.pre_state.get("provider_parent_id")
        if expected_parent and current.parent_ids and expected_parent not in current.parent_ids:
            raise ConflictError(
                "The file was moved since this was reviewed — refusing to act on it. Rescan "
                "and review it again."
            )

    def _reject_if_protected(self, step: ExecutionStep, file: File) -> None:
        """The last line of the protection policy (see
        `vault_shared.protection`): whatever path built this plan,
        a protected file is never removed or relocated unless the user who
        picked it acknowledged that, as recorded on the step itself."""
        if not guards(step.action_type, step.planned_change):
            return
        if step.planned_change.get("protection_override"):
            return
        reasons = self._protection.reasons(file)
        if reasons:
            raise ConflictError(
                f"{file.name} is protected ({'; '.join(reasons)}) — it was left untouched."
            )

    @staticmethod
    def _observe_progress(
        step: ExecutionStep, current: StorageFile | None, pre_state: dict
    ) -> _StepProgress:
        """State-based retry detection — not 'swallow the error'. A retry
        may treat a step as already applied only when the live file is in
        the state the step produces; if it is still in the captured
        pre-state the step is simply not applied yet; anything else means
        somebody else changed the file in the meantime, and overwriting that
        would be a guess. (When another actor happened to reach the exact
        target state, the outcome is identical to ours and is reported as
        already applied — the audit trail says so.)"""
        action_type = step.action_type
        if action_type == ExecutionActionType.PERMANENT_DELETE:
            if current is None:
                return _StepProgress.APPLIED
            return _StepProgress.NOT_APPLIED if current.trashed else _StepProgress.CONFLICT
        if current is None:
            return _StepProgress.CONFLICT
        if action_type in _TRASHABLE_ACTIONS:
            return _StepProgress.APPLIED if current.trashed else _StepProgress.NOT_APPLIED
        if action_type == ExecutionActionType.RESTORE:
            return _StepProgress.NOT_APPLIED if current.trashed else _StepProgress.APPLIED
        if action_type == ExecutionActionType.RENAME:
            if current.name == step.planned_change.get("new_name"):
                return _StepProgress.APPLIED
            if current.name == pre_state.get("name"):
                return _StepProgress.NOT_APPLIED
            return _StepProgress.CONFLICT
        if action_type in _MOVE_ACTIONS:
            if step.planned_change.get("new_parent_id") in current.parent_ids:
                return _StepProgress.APPLIED
            if pre_state.get("parent_folder_id") in current.parent_ids:
                return _StepProgress.NOT_APPLIED
            return _StepProgress.CONFLICT
        # `appProperties` writes set fixed values, so repeating one is
        # already idempotent.
        return _StepProgress.NOT_APPLIED

    def _reconcile_local_mirror(
        self, step: ExecutionStep, file: File, current: StorageFile | None
    ) -> None:
        """The provider write of a crashed earlier attempt landed but its local
        bookkeeping did not — bring the mirror in line without a second
        write."""
        if step.action_type in _TRASHABLE_ACTIONS:
            self._files.mark_trashed(file, trashed=True)
        elif step.action_type == ExecutionActionType.RESTORE:
            self._files.mark_trashed(file, trashed=False)
        elif (
            step.action_type == ExecutionActionType.PERMANENT_DELETE
            and file.permanently_deleted_at is None
        ):
            self._files.mark_permanently_deleted(file)
        elif current is not None and (
            step.action_type == ExecutionActionType.RENAME or step.action_type in _MOVE_ACTIONS
        ):
            self._mirror_location(file, current)

    def _mirror_location(self, file: File, provider_state: StorageFile) -> None:
        """Provider state → AI Vault state, after a verified-landed rename or
        move: the local row takes the name and parent the provider reports,
        never the ones the step *intended*. Without this the UI keeps showing
        the pre-mutation name/location until the next scan."""
        parent_folder = self._folders.get_by_source_and_provider_parent(
            storage_source_id=file.storage_source_id, provider_file_id=provider_state.parent_id
        )
        path = (
            f"{parent_folder.path.rstrip('/')}/{provider_state.name}"
            if parent_folder is not None
            else f"/{provider_state.name}"
        )
        self._files.mirror_location(
            file,
            name=provider_state.name,
            provider_parent_id=provider_state.parent_id,
            parent_folder_id=parent_folder.id if parent_folder is not None else None,
            path=path,
        )

    @staticmethod
    def _pre_mutation_state(action_type: str, current: StorageFile) -> dict:
        if action_type in _TRASHABLE_ACTIONS:
            return {"trashed": False}
        if action_type == ExecutionActionType.RESTORE:
            return {"trashed": True}
        if action_type == ExecutionActionType.RENAME:
            return {"name": current.name}
        if action_type in _MOVE_ACTIONS:
            return {"parent_folder_id": current.parent_id}
        return {}

    def _apply_action(
        self, step: ExecutionStep, *, adapter: StorageAdapter, file: File, current: StorageFile
    ) -> None:
        action_type = step.action_type
        file_id = ProviderFileId(file.provider_file_id)
        if action_type in _TRASHABLE_ACTIONS:
            adapter.trash(file_id)
            # The local mirror's only record that this file left active
            # storage — excluded from every Storage Intelligence/Dashboard
            # total from here on (FileRepository._for_organization etc.),
            # without deleting the row (see File.trashed's docstring on why).
            self._files.mark_trashed(file, trashed=True)
        elif action_type == ExecutionActionType.RESTORE:
            adapter.restore(file_id)
            self._files.mark_trashed(file, trashed=False)
        elif action_type == ExecutionActionType.RENAME:
            new_name = step.planned_change.get("new_name")
            if not new_name:
                raise ValidationError("Rename step has no target name.")
            self._mirror_location(file, adapter.rename(file_id, new_name))
        elif action_type in _MOVE_ACTIONS:
            new_parent_id = step.planned_change.get("new_parent_id")
            old_parent_id = current.parent_id
            if not new_parent_id or not old_parent_id:
                raise ValidationError("Move step is missing a source or target parent folder.")
            moved = adapter.move(
                file_id,
                new_parent_id=ProviderFileId(new_parent_id),
                old_parent_id=old_parent_id,
            )
            self._mirror_location(file, moved)
        elif action_type == ExecutionActionType.UPDATE_METADATA:
            properties = step.planned_change.get("properties")
            if not properties:
                raise ValidationError("Update-metadata step has no properties to set.")
            adapter.update_metadata(file_id, properties)
        elif action_type == ExecutionActionType.PERMANENT_DELETE:
            adapter.permanent_delete(file_id)
            self._files.mark_permanently_deleted(file)
        else:
            raise ValidationError(f"Unsupported action type: {action_type}")

    # ------------------------------------------------------------------
    # Archive MVP — one batch operation completing N CREATE_ARCHIVE steps
    # ------------------------------------------------------------------

    def _open_for_archive(
        self, file: File, *, adapter: StorageAdapter
    ) -> tuple[Iterable[bytes], str | None] | None:
        """Returns `(byte_stream, export_extension)`, or `None` for a file this
        archive can't meaningfully contain — a provider-native document with
        no exportable format (forms, sites, drawings) or a folder. Callers
        treat that as an individual step failure, not a batch failure."""
        mime = file.mime_type or ""
        file_id = ProviderFileId(file.provider_file_id)
        export_format = adapter.export_format_for(mime, ExportPurpose.ARCHIVE)
        if export_format is not None:
            return adapter.export(file_id, export_format), export_format.extension
        if adapter.is_native_document(mime):
            return None
        return adapter.open_read(file_id), None

    def _fail_archive_step(
        self, job: ExecutionJob, plan: ExecutionPlan, step: ExecutionStep, error: str
    ) -> None:
        now = datetime.now(UTC)
        self._steps.mark_failed(step)
        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.FAILED,
            verification_status=VerificationStatus.SKIPPED,
            error=error,
            executed_at=now,
            verified_at=None,
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="step_failed",
            message=error,
            metadata={"execution_step_id": str(step.id), "action_type": step.action_type},
        )

    def _ensure_folder_path(
        self, *, organization_id: uuid.UUID, storage_source_id: uuid.UUID, names: list[str]
    ) -> Folder:
        parent: Folder | None = None
        for name in names:
            existing = self._folders.get_by_parent_and_name(
                storage_source_id=storage_source_id,
                parent_folder_id=parent.id if parent else None,
                name=name,
            )
            parent = existing or self.create_folder(
                organization_id=organization_id,
                storage_source_id=storage_source_id,
                name=name,
                parent_folder_id=parent.id if parent else None,
            )
        assert parent is not None
        return parent

    def _chosen_archive_folder(self, folder_id: str | None, first_file: File) -> Folder | None:
        if not folder_id:
            return None
        folder = self._folders.get_by_id(uuid.UUID(folder_id))
        if folder is None or folder.storage_source_id != first_file.storage_source_id:
            raise ValidationError(
                "The chosen destination folder is no longer in this storage — choose another."
            )
        return folder

    def _execute_archive_batch(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        adapter: StorageAdapter,
    ) -> None:
        """Completes every `CREATE_ARCHIVE` step in `steps` together as one
        archive, in this order — never another:

        1. compress the files into one zip (with a `manifest.json` inside);
        2. upload it into the user's own storage, under `AI Vault Archive/<year>/`;
        3. verify the provider now holds exactly those bytes (size + MD5);
        4. only then, and only if the user asked, move the originals to Trash,
           confirming each one with the provider.

        If anything in 1-3 fails, every original is left untouched. Per-file
        problems (missing, oversized, unexportable) fail only that file's
        step; a failure in the shared part fails the whole batch."""
        archive_job = self._archive_jobs.get_by_plan_id(plan.id) or self._archive_jobs.create(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            name=f"Archive - {plan.estimated_impact}",
            created_by_user_id=plan.created_by_user_id,
        )
        remove_originals = any(step.planned_change.get("remove_originals") for step in steps)
        archive_job.remove_originals = remove_originals
        self._archive_jobs.mark_creating(archive_job)
        self._db.commit()

        started_at = datetime.now(UTC)
        requested = steps[0].planned_change if steps else {}
        archive_name = (
            requested.get("archive_name")
            or f"AI Vault Archive {started_at:%Y-%m-%d %H%M%S} ({len(steps)} files).zip"
        )
        included: list[tuple[ExecutionStep, File]] = []
        manifest: list[dict] = []
        # Closed in the `finally` below (and by boto3 on upload) — a `with` block
        # here would re-indent the whole batch for no behavioral gain.
        buffer = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=64 * 1024 * 1024, dir=_archive_temp_dir()
        )
        try:
            try:
                first_file = self._first_archivable_file(plan, steps)
                destination = self._chosen_archive_folder(
                    requested.get("archive_folder_id"), first_file
                ) or self._ensure_folder_path(
                    organization_id=plan.organization_id,
                    storage_source_id=first_file.storage_source_id,
                    names=["AI Vault Archive", f"{started_at:%Y}"],
                )
                # Never overwrite: a name already in that folder gets a number.
                archive_name = numbered_archive_name(
                    archive_name,
                    self._files.names_in_folder(destination.storage_source_id, destination.id),
                )
                built = (
                    self._build_native_archive(
                        job,
                        plan,
                        steps,
                        adapter=adapter,
                        destination=destination,
                        archive_name=archive_name,
                        started_at=started_at,
                    )
                    if isinstance(adapter, NativeArchiver)
                    else self._build_zip_archive(
                        job,
                        plan,
                        steps,
                        adapter=adapter,
                        buffer=buffer,
                        destination=destination,
                        archive_name=archive_name,
                        started_at=started_at,
                    )
                )
                included, manifest, stored = built.included, built.manifest, built.stored
                original_size, compressed_size = built.original_size, built.compressed_size
                md5, sha256 = built.md5, built.sha256
                verified_at = datetime.now(UTC)
                # Shown in Files right away, in the folder it was saved to.
                self._record_new_file(destination.storage_source_id, destination, stored)
            except Exception as exc:  # noqa: BLE001 - archive-batch boundary must never crash the worker
                logger.exception("archive_batch_failed", extra={"execution_plan_id": str(plan.id)})
                # rollback() expires every object in the session, so any
                # individual _fail_archive_step calls already flushed above
                # (but never committed) are undone too, and every step is
                # failed together below — one shared failure fails the batch.
                self._db.rollback()
                self._archive_jobs.mark_failed(archive_job)
                for step in steps:
                    self._fail_archive_step(job, plan, step, str(exc))
                self._db.commit()
                return

            # A secondary copy for in-app download only. The archive of
            # record is the verified file in the user's own storage, so a
            # cache-write failure does not fail the archive; large archives
            # are opened from the provider instead of being duplicated here.
            object_storage_key: str | None = None
            if built.cacheable and compressed_size <= _MAX_CACHED_ARCHIVE_BYTES:
                object_storage_key = f"archives/{plan.organization_id}/{plan.id}.zip"
                try:
                    buffer.seek(0)
                    self._object_storage.put_object(
                        key=object_storage_key, body=buffer, content_type="application/zip"
                    )
                except Exception:  # noqa: BLE001 - optional cache
                    logger.exception(
                        "archive_cache_write_failed", extra={"execution_plan_id": str(plan.id)}
                    )
                    object_storage_key = None
        finally:
            buffer.close()

        removed_file_ids = (
            self._remove_archived_originals(included, adapter=adapter)
            if remove_originals
            else set()
        )
        for entry in manifest:
            entry["original_removed"] = entry["file_id"] in removed_file_ids

        self._archive_jobs.mark_completed(
            archive_job,
            object_storage_key=object_storage_key,
            original_size_bytes=original_size,
            compressed_size_bytes=compressed_size,
            file_count=len(manifest),
            manifest=manifest,
            destination_provider_file_id=stored.provider_file_id,
            destination_path=f"{destination.path.rstrip('/')}/{archive_name}",
            destination_web_view_link=stored.web_view_link,
            archive_md5=md5,
            archive_sha256=sha256,
            verified_at=verified_at,
            originals_removed_count=len(removed_file_ids),
        )

        now = datetime.now(UTC)
        for step, file in included:
            original_removed = str(file.id) in removed_file_ids
            self._rollback_records.create(
                execution_step_id=step.id,
                pre_state={
                    "archive_job_id": str(archive_job.id),
                    "original_removed": original_removed,
                },
            )
            self._steps.mark_completed(step)
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.SUCCESS,
                verification_status=VerificationStatus.VERIFIED,
                error=None,
                executed_at=now,
                verified_at=verified_at,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_completed",
                metadata={
                    "execution_step_id": str(step.id),
                    "action_type": step.action_type,
                    "archive_job_id": str(archive_job.id),
                    "original_removed": original_removed,
                },
            )
        self._db.commit()

    def _build_zip_archive(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        adapter: StorageAdapter,
        buffer: IO[bytes],
        destination: Folder,
        archive_name: str,
        started_at: datetime,
    ) -> "_BuiltArchive":
        """Builds the zip here, uploads it, and checks the provider holds
        exactly those bytes. Each file keeps its place in the folder
        structure (`_archive_name_for`); already-compressed files are
        stored, everything else deflated at the highest level."""
        manifest: list[dict] = []
        included: list[tuple[ExecutionStep, File]] = []
        skipped_reasons: list[str] = []
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
            used_names: set[str] = set()
            for step in steps:
                try:
                    file = self._get_plan_file(plan, step)
                except NotFoundError:
                    reason = "Target file no longer exists in this platform's inventory."
                    skipped_reasons.append(reason)
                    self._fail_archive_step(job, plan, step, reason)
                    continue
                try:
                    _require_free_space(file.size_bytes or 0, what=f"archive '{file.name}'")
                except ValidationError as exc:
                    skipped_reasons.append(str(exc))
                    self._fail_archive_step(job, plan, step, str(exc))
                    continue

                try:
                    opened = self._open_for_archive(file, adapter=adapter)
                except Exception as exc:  # noqa: BLE001 - one file's fetch failure must not stop the batch
                    skipped_reasons.append(f"'{file.name}': {exc}")
                    self._fail_archive_step(job, plan, step, str(exc))
                    continue
                if opened is None:
                    reason = f"'{file.name}' is a file type that can't be archived."
                    skipped_reasons.append(reason)
                    self._fail_archive_step(job, plan, step, reason)
                    continue
                stream, exported_as = opened

                in_archive = _archive_name_for(step, file)
                base_name = f"{in_archive}.{exported_as}" if exported_as else in_archive
                zip_name = base_name
                suffix = 1
                while zip_name in used_names:
                    suffix += 1
                    zip_name = f"{suffix} - {base_name}"
                used_names.add(zip_name)

                size_written, entry_sha256 = _write_zip_entry(
                    zip_file,
                    zip_name,
                    stream,
                    store_only=_is_already_compressed(base_name),
                )
                manifest.append(
                    {
                        "file_id": str(file.id),
                        "name": file.name,
                        "path": file.path,
                        "provider": plan.target_provider,
                        "provider_file_id": file.provider_file_id,
                        "original_size_bytes": file.size_bytes,
                        "original_modified_at": (
                            file.provider_modified_at.isoformat()
                            if file.provider_modified_at
                            else None
                        ),
                        "size_bytes": size_written,
                        "mime_type": file.mime_type,
                        "exported_as": exported_as,
                        "zip_entry_name": zip_name,
                        "checksum_sha256": entry_sha256,
                        "archived_at": started_at.isoformat(),
                        "original_removed": False,
                    }
                )
                included.append((step, file))

            if not manifest:
                raise ValidationError(
                    " ".join(skipped_reasons)
                    or "No selected file could be included in the archive."
                )
            zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))

        _verify_zip(buffer, expected_entries=len(manifest) + 1)
        original_size = sum(entry["size_bytes"] for entry in manifest)
        compressed_size = buffer.seek(0, 2)
        md5, sha256 = _digests(buffer)

        buffer.seek(0)
        uploaded = adapter.upload(
            archive_name,
            _iter_chunks(buffer),
            mime_type="application/zip",
            parent_id=ProviderFileId(destination.provider_file_id),
        )
        try:
            stored = adapter.get_file(uploaded.provider_file_id)
            if stored.size_bytes != compressed_size or (
                stored.checksum is not None and stored.checksum != md5
            ):
                raise ValidationError(
                    "The archive stored in your connected storage does not match what "
                    "was written — originals were left untouched."
                )
        except Exception:
            _discard_unverified(adapter, uploaded)
            raise
        return _BuiltArchive(
            included=included,
            manifest=manifest,
            stored=stored,
            original_size=original_size,
            compressed_size=compressed_size,
            md5=md5,
            sha256=sha256,
            cacheable=True,
        )

    def _build_native_archive(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        adapter: "NativeArchiver",
        destination: Folder,
        archive_name: str,
        started_at: datetime,
    ) -> "_BuiltArchive":
        """The storage builds and checks the archive itself (this computer's
        agent): the files never leave the disk. The result is then read back
        from the storage and compared with what the agent reported."""
        by_provider_id: dict[str, tuple[ExecutionStep, File]] = {}
        items: list[tuple[ProviderFileId, str]] = []
        for step in steps:
            try:
                file = self._get_plan_file(plan, step)
            except NotFoundError:
                self._fail_archive_step(
                    job, plan, step, "Target file no longer exists in this platform's inventory."
                )
                continue
            by_provider_id[file.provider_file_id] = (step, file)
            items.append((ProviderFileId(file.provider_file_id), _archive_name_for(step, file)))
        if not items:
            raise ValidationError("None of the selected files are still in this storage.")

        result = adapter.create_archive(
            items,
            parent_id=ProviderFileId(destination.provider_file_id),
            name=archive_name,
            expected_bytes=sum(file.size_bytes or 0 for _, file in by_provider_id.values()),
        )
        try:
            stored = adapter.get_file(result.file.provider_file_id)
            # The agent has already read every entry back against its CRC;
            # this confirms the file it reported is the one on disk now.
            if stored.size_bytes != result.file.size_bytes or (
                stored.checksum is not None
                and result.file.checksum is not None
                and stored.checksum != result.file.checksum
            ):
                raise ValidationError(
                    "The archive on disk does not match what was written — originals were "
                    "left untouched."
                )
        except Exception:
            _discard_unverified(adapter, result.file)
            raise

        written = {entry["file_id"]: entry for entry in result.entries}
        included: list[tuple[ExecutionStep, File]] = []
        manifest: list[dict] = []
        for provider_id, (step, file) in by_provider_id.items():
            entry = written.get(provider_id)
            if entry is None:
                reason = next(
                    (
                        item["reason"]
                        for item in result.skipped
                        if item.get("file_id") == provider_id
                    ),
                    "It could not be added to the archive.",
                )
                self._fail_archive_step(job, plan, step, f"'{file.name}': {reason}")
                continue
            included.append((step, file))
            manifest.append(
                {
                    "file_id": str(file.id),
                    "name": file.name,
                    "path": file.path,
                    "provider": plan.target_provider,
                    "provider_file_id": file.provider_file_id,
                    "original_size_bytes": file.size_bytes,
                    "size_bytes": entry["size_bytes"],
                    "compressed_size_bytes": entry["compressed_bytes"],
                    "stored_uncompressed": entry["stored"],
                    "zip_entry_name": entry["arcname"],
                    "archived_at": started_at.isoformat(),
                    "original_removed": False,
                }
            )
        if not manifest:
            raise ValidationError("None of the selected files could be added to the archive.")
        return _BuiltArchive(
            included=included,
            manifest=manifest,
            stored=stored,
            original_size=sum(entry["size_bytes"] for entry in manifest),
            compressed_size=stored.size_bytes or 0,
            md5=None,
            sha256=result.file.checksum,
            cacheable=False,
        )

    def _first_archivable_file(self, plan: ExecutionPlan, steps: list[ExecutionStep]) -> File:
        """The first selected file still in this storage — where the archive
        goes. When none is, says why for each file instead of a generic
        refusal."""
        reasons: list[str] = []
        for step in steps:
            try:
                return self._get_plan_file(plan, step)
            except NotFoundError as error:
                reasons.append(str(error))
        raise ValidationError(
            "No selected file could be included in the archive: " + "; ".join(sorted(set(reasons)))
        )

    def _remove_archived_originals(
        self, included: list[tuple[ExecutionStep, File]], *, adapter: StorageAdapter
    ) -> set[str]:
        """Runs only after the archive is verified in the provider. Each
        original is moved to Trash (recoverable) and confirmed trashed by
        the provider; one that fails stays where it was and is reported as
        not removed — never as removed."""
        removed: set[str] = set()
        for step, file in included:
            if file.trashed:
                continue
            if not step.planned_change.get("protection_override") and self._protection.reasons(
                file
            ):
                continue
            file_id = ProviderFileId(file.provider_file_id)
            try:
                adapter.trash(file_id)
                if adapter.get_file(file_id).trashed:
                    self._files.mark_trashed(file, trashed=True)
                    removed.add(str(file.id))
            except Exception:  # noqa: BLE001 - one original's removal must not undo a verified archive
                logger.exception("archive_original_removal_failed", extra={"file_id": str(file.id)})
        return removed

    def _verify_forward(self, step: ExecutionStep, *, adapter: StorageAdapter, file: File) -> bool:
        file_id = ProviderFileId(file.provider_file_id)
        if step.action_type == ExecutionActionType.PERMANENT_DELETE:
            # The success signal here is the opposite of every other
            # action's: "not found" *is* verification, not a failure.
            try:
                adapter.get_file(file_id)
            except NotFoundError:
                return True
            return False

        current = adapter.get_file(file_id)
        if step.action_type in _TRASHABLE_ACTIONS:
            return current.trashed is True
        if step.action_type == ExecutionActionType.RESTORE:
            return current.trashed is False
        if step.action_type == ExecutionActionType.RENAME:
            expected = step.planned_change.get("new_name")
            return expected is None or current.name == expected
        if step.action_type in _MOVE_ACTIONS:
            expected = step.planned_change.get("new_parent_id")
            return expected is None or expected in current.parent_ids
        return True

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _run_rollback(
        self, job: ExecutionJob, plan: ExecutionPlan, *, adapter: StorageAdapter
    ) -> None:
        records = self._rollback_records.list_rollbackable_for_plan(plan.id)
        succeeded = 0
        failed = 0
        trashable_restored = False
        for record in records:
            self._check_cancelled(job.id)
            if self._jobs.is_pause_requested(job.id):
                self._jobs.mark_paused(job)
                self._audits.record(
                    organization_id=plan.organization_id,
                    execution_plan_id=plan.id,
                    execution_job_id=job.id,
                    event_type="execution_paused",
                )
                self._db.commit()
                raise ExecutionPaused

            step = self._steps.get_by_id(record.execution_step_id)
            if step is None:
                failed += 1
                continue

            if self._rollback_one(job, plan, step, record, adapter=adapter):
                succeeded += 1
                if step.action_type in _TRASHABLE_ACTIONS:
                    trashable_restored = True
            else:
                failed += 1

        if failed == 0 and succeeded > 0:
            self._jobs.mark_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.ROLLED_BACK)
        elif succeeded == 0:
            self._jobs.mark_failed(job, error="All rollback steps failed.")
        else:
            self._jobs.mark_partially_completed(job)

        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_rollback_completed",
            metadata={"succeeded": succeeded, "failed": failed},
        )
        self._db.commit()

        # Un-trashing restores the file to active storage — the totals need
        # to come back up, same as they needed to go down when it was
        # trashed in the first place.
        if trashable_restored:
            self._trigger_storage_refresh(plan.organization_id)

    def _rollback_one(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        step: ExecutionStep,
        record: RollbackRecord,
        *,
        adapter: StorageAdapter,
    ) -> bool:
        now = datetime.now(UTC)
        try:
            file = self._get_plan_file(plan, step)

            self._apply_rollback(step, adapter=adapter, file=file, pre_state=record.pre_state)
            self._rollback_records.mark_rolled_back(record, user_id=job.triggered_by_user_id)
            self._steps.mark_rolled_back(step)
        except Exception as exc:  # noqa: BLE001 - one file's rollback failure must not stop the rest
            logger.exception(
                "execution_rollback_step_failed", extra={"execution_step_id": str(step.id)}
            )
            self._db.rollback()
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.FAILED,
                verification_status=VerificationStatus.SKIPPED,
                error=str(exc),
                executed_at=now,
                verified_at=None,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="rollback_step_failed",
                message=str(exc),
                metadata={"execution_step_id": str(step.id)},
            )
            self._db.commit()
            return False

        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.SUCCESS,
            verification_status=VerificationStatus.VERIFIED,
            error=None,
            executed_at=now,
            verified_at=datetime.now(UTC),
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="rollback_step_completed",
            metadata={"execution_step_id": str(step.id)},
        )
        self._db.commit()
        return True

    def _apply_rollback(
        self, step: ExecutionStep, *, adapter: StorageAdapter, file: File, pre_state: dict
    ) -> None:
        """Rollback is retryable and never clobbers a third party's change:
        a file already in its pre-state is left alone (a crashed earlier
        rollback attempt), and one that is neither in its pre-state nor in
        the state this step produced was changed by someone else since."""
        action_type = step.action_type
        file_id = ProviderFileId(file.provider_file_id)
        if action_type in _TRASHABLE_ACTIONS:
            adapter.restore(file_id)
            self._files.mark_trashed(file, trashed=False)
        elif action_type == ExecutionActionType.RESTORE:
            adapter.trash(file_id)
            self._files.mark_trashed(file, trashed=True)
        elif action_type == ExecutionActionType.RENAME:
            current = adapter.get_file(file_id)
            if current.name == pre_state["name"]:
                self._mirror_location(file, current)
                return
            if current.name != step.planned_change.get("new_name"):
                raise ConflictError(
                    "The file was renamed by someone else after this step — refusing to overwrite."
                )
            self._mirror_location(file, adapter.rename(file_id, pre_state["name"]))
        elif action_type in _MOVE_ACTIONS:
            current = adapter.get_file(file_id)
            current_parent_id = current.parent_id
            original_parent_id = pre_state.get("parent_folder_id")
            if not current_parent_id or not original_parent_id:
                raise ValidationError("Cannot determine parents to reverse this move.")
            if original_parent_id in current.parent_ids:
                self._mirror_location(file, current)
                return
            if step.planned_change.get("new_parent_id") not in current.parent_ids:
                raise ConflictError(
                    "The file was moved by someone else after this step — refusing to overwrite."
                )
            moved_back = adapter.move(
                file_id,
                new_parent_id=ProviderFileId(original_parent_id),
                old_parent_id=current_parent_id,
            )
            self._mirror_location(file, moved_back)
        elif action_type == ExecutionActionType.UPDATE_METADATA:
            adapter.update_metadata(file_id, pre_state.get("app_properties", {}))
        elif action_type == ExecutionActionType.CREATE_ARCHIVE:
            # No provider call — CREATE_ARCHIVE never mutated the provider,
            # only read from it, so `file` isn't touched here at all.
            if pre_state.get("original_removed"):
                adapter.restore(file_id)
                self._files.mark_trashed(file, trashed=False)
            self._rollback_archive(pre_state["archive_job_id"], adapter=adapter)
        elif action_type == ExecutionActionType.PERMANENT_DELETE:
            # Unreachable in practice — every permanent-delete plan is
            # created with rollback_available=False, and
            # ExecutionJobService.trigger_rollback rejects the request
            # before a job (and this call) ever exists. Kept as an
            # explicit, honest failure rather than silently no-op-ing.
            raise ValidationError("Permanently deleted files cannot be restored.")
        else:
            raise ValidationError(f"Unsupported action type: {action_type}")

    def _rollback_archive(self, archive_job_id: str, *, adapter: StorageAdapter) -> None:
        """Every sibling `CREATE_ARCHIVE` step in a plan shares one
        `ArchiveJob`, so this runs once per step during a plan rollback —
        idempotent via the status check, since the archive is only ever
        removed once. The archive in the user's storage goes to Trash
        (recoverable), never permanently deleted."""
        archive_job = self._archive_jobs.get_by_id(uuid.UUID(archive_job_id))
        if archive_job is None or archive_job.status != ArchiveJobStatus.COMPLETED:
            return
        if archive_job.destination_provider_file_id:
            adapter.trash(ProviderFileId(archive_job.destination_provider_file_id))
        if archive_job.object_storage_key:
            self._object_storage.delete_object(key=archive_job.object_storage_key)
        self._archive_jobs.mark_failed(archive_job)

    def _check_cancelled(self, execution_job_id: uuid.UUID) -> None:
        if self._jobs.is_cancel_requested(execution_job_id):
            raise ExecutionCancelled

    def _trigger_storage_refresh(self, organization_id: uuid.UUID) -> None:
        """A trash (or its rollback) just changed which files count toward
        active storage — without this, Storage Intelligence's totals and
        the Dashboard's `total_storage_bytes` would keep reporting the
        pre-trash number until the next scan or manual re-analysis (see
        `File.trashed`'s docstring). Mirrors the exact enqueue-guarded-by-
        has_active_job pattern `worker.tasks.scan`/`worker.tasks.embedding`
        already use for their own completion-chained triggers — a failure
        to enqueue either (e.g. one already running) is not itself a
        problem worth failing the execution job over, so both are best-
        effort here."""
        if not self._storage_analysis_jobs.has_active_job(organization_id):
            analysis_job = self._storage_analysis_jobs.create(
                organization_id=organization_id,
                triggered_by=StorageAnalysisTrigger.EXECUTION_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            run_storage_intelligence.delay(str(analysis_job.id))

        if not self._recommendation_jobs.has_active_job(organization_id):
            recommendation_job = self._recommendation_jobs.create(
                organization_id=organization_id,
                triggered_by=RecommendationTrigger.EXECUTION_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            run_recommendation.delay(str(recommendation_job.id))
