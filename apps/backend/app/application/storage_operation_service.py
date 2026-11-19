import uuid

from sqlalchemy.orm import Session

from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.queue.storage_operation_producer import (
    CREATE_FOLDER_TASK,
    CREATE_TEXT_FILE_TASK,
    EMPTY_TRASH_TASK,
    EXTRACT_ARCHIVE_TASK,
    INSPECT_ARCHIVE_TASK,
    MAKE_COPY_TASK,
    PREVIEW_TRASH_TASK,
    run_storage_operation,
    start_storage_operation,
    storage_operation_result,
)
from vault_shared import NotFoundError, ValidationError
from vault_shared.ai_gateway.recommendation import validate_component_name
from vault_shared.db.repositories import (
    AuditLogRepository,
    FileRepository,
    FolderRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)

# One emptying per connection at a time; a repeated click joins the one
# already running instead of starting another.
_EMPTY_TRASH_LOCK_SECONDS = 15 * 60
_TEXT_FORMATS = {"text": ("text/plain", ".txt"), "markdown": ("text/markdown", ".md")}


class StorageOperationService:
    """Creating things in the user's storage — folders and simple text
    files. The mutation itself happens in the worker's Execution Engine;
    this service validates ownership and input, then reports the
    provider-confirmed result."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connectors = StorageConnectorRepository(db)
        self._sources = StorageSourceRepository(db)
        self._folders = FolderRepository(db)
        self._files = FileRepository(db)
        self._audit_logs = AuditLogRepository(db)

    def create_folder(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        parent_folder_id: uuid.UUID | None,
        source_id: uuid.UUID | None = None,
    ) -> dict:
        self._check_owned(connector_id, organization_id=organization_id, folder_id=parent_folder_id)
        result = run_storage_operation(
            CREATE_FOLDER_TASK,
            args=[
                str(organization_id),
                str(connector_id),
                _valid_name(name),
                str(parent_folder_id) if parent_folder_id else None,
                str(source_id) if source_id else None,
            ],
        )
        return self._finish(
            result, organization_id=organization_id, user_id=user_id, event="folder_created"
        )

    def create_text_file(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        content: str,
        text_format: str,
        parent_folder_id: uuid.UUID | None,
        source_id: uuid.UUID | None = None,
    ) -> dict:
        if text_format not in _TEXT_FORMATS:
            raise ValidationError("Choose plain text or Markdown.")
        self._check_owned(connector_id, organization_id=organization_id, folder_id=parent_folder_id)
        mime_type, extension = _TEXT_FORMATS[text_format]
        file_name = _valid_name(name)
        if not file_name.lower().endswith(extension):
            file_name = f"{file_name}{extension}"
        result = run_storage_operation(
            CREATE_TEXT_FILE_TASK,
            args=[
                str(organization_id),
                str(connector_id),
                file_name,
                content,
                mime_type,
                str(parent_folder_id) if parent_folder_id else None,
                str(source_id) if source_id else None,
            ],
        )
        return self._finish(
            result, organization_id=organization_id, user_id=user_id, event="file_created"
        )

    def make_copy(
        self,
        file_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        new_name: str | None,
    ) -> dict:
        if not self._files.list_owned_by_organization([file_id], organization_id=organization_id):
            raise NotFoundError("File not found.")
        result = run_storage_operation(
            MAKE_COPY_TASK,
            args=[str(organization_id), str(file_id), _valid_name(new_name) if new_name else None],
        )
        return self._finish(
            result, organization_id=organization_id, user_id=user_id, event="file_copied"
        )

    def extract_archive(
        self,
        file_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        destination_folder_id: uuid.UUID | None,
    ) -> dict:
        """Unpacks a zip into a new folder in the same storage; answers only
        once the storage has confirmed what was created."""
        if not self._files.list_owned_by_organization([file_id], organization_id=organization_id):
            raise NotFoundError("File not found.")
        result = run_storage_operation(
            EXTRACT_ARCHIVE_TASK,
            args=[
                str(organization_id),
                str(file_id),
                str(destination_folder_id) if destination_folder_id else None,
            ],
            wait_seconds=_EXTRACT_WAIT_SECONDS,
        )
        return self._finish(
            result, organization_id=organization_id, user_id=user_id, event="archive_extracted"
        )

    def inspect_archive(self, file_id: uuid.UUID, *, organization_id: uuid.UUID) -> dict:
        """What a zip holds, read from the storage — nothing is changed."""
        if not self._files.list_owned_by_organization([file_id], organization_id=organization_id):
            raise NotFoundError("File not found.")
        result = run_storage_operation(
            INSPECT_ARCHIVE_TASK,
            args=[str(organization_id), str(file_id)],
            wait_seconds=_EXTRACT_WAIT_SECONDS,
        )
        if not result.get("ok"):
            raise ValidationError(str(result.get("error") or "Couldn't read that archive."))
        return result

    def preview_trash(self, connector_id: uuid.UUID, *, organization_id: uuid.UUID) -> dict:
        self._check_owned(connector_id, organization_id=organization_id, folder_id=None)
        result = run_storage_operation(
            PREVIEW_TRASH_TASK, args=[str(organization_id), str(connector_id)]
        )
        if not result.get("ok"):
            raise ValidationError(str(result.get("error") or "Couldn't read Drive's Trash."))
        return result

    def start_empty_trash(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        expected_count: int,
        confirmed: bool,
    ) -> str:
        """Irreversible: the user's explicit acknowledgement and the reviewed
        file count are both required, so a stray click or a stale screen
        can't delete anything. Returns the id to poll for the outcome."""
        if not confirmed:
            raise ValidationError(
                "Confirm that you understand these files will be permanently deleted."
            )
        self._check_owned(connector_id, organization_id=organization_id, folder_id=None)
        redis = get_redis()
        lock = _empty_trash_lock(connector_id)
        running = redis.get(lock)
        if running:
            return str(running)
        task_id = start_storage_operation(
            EMPTY_TRASH_TASK, args=[str(organization_id), str(connector_id), expected_count]
        )
        if not redis.set(lock, task_id, nx=True, ex=_EMPTY_TRASH_LOCK_SECONDS):
            # Another request won the race; both now follow the same run.
            return str(redis.get(lock) or task_id)
        self._audit_logs.record(
            event_type="trash_empty_requested",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector_id), "expected_count": expected_count},
        )
        self._db.commit()
        return task_id

    def empty_trash_status(
        self, connector_id: uuid.UUID, task_id: str, *, organization_id: uuid.UUID
    ) -> dict:
        """`{"status": "running"}` until the worker finishes, then the
        provider-confirmed outcome. Only the connection that started the run
        can read it."""
        self._check_owned(connector_id, organization_id=organization_id, folder_id=None)
        result = storage_operation_result(task_id)
        if result is None:
            return {"status": "running"}
        if result.get("connector_id") not in (None, str(connector_id)):
            raise NotFoundError("That operation was not found.")
        redis = get_redis()
        lock = _empty_trash_lock(connector_id)
        if redis.get(lock) == task_id:
            redis.delete(lock)
        if not result.get("ok"):
            return {
                "status": "failed",
                "error": str(result.get("error") or "Couldn't empty Trash."),
            }
        return {"status": "done", "summary": result}

    def _check_owned(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, folder_id: uuid.UUID | None
    ) -> None:
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connection not found.")
        if folder_id is not None:
            folder = self._folders.get_by_id(folder_id)
            source = self._sources.get_by_id(folder.storage_source_id) if folder else None
            if source is None or source.connector_id != connector_id:
                raise NotFoundError("Folder not found.")

    def _finish(
        self, result: dict, *, organization_id: uuid.UUID, user_id: uuid.UUID, event: str
    ) -> dict:
        if not result.get("ok"):
            raise ValidationError(str(result.get("error") or "The operation failed."))
        self._audit_logs.record(
            event_type=event,
            organization_id=organization_id,
            user_id=user_id,
            metadata={"id": result.get("id"), "name": result.get("name")},
        )
        self._db.commit()
        return result


# Unpacking runs through the storage file by file; it gets longer than a
# single quick operation before the request gives up waiting.
_EXTRACT_WAIT_SECONDS = 600


def _valid_name(name: str) -> str:
    try:
        return validate_component_name(name.strip(), what="Name")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _empty_trash_lock(connector_id: uuid.UUID) -> str:
    return f"empty-trash:{connector_id}"
