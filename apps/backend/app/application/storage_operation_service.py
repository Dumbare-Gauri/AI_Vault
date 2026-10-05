import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.storage_operation_producer import (
    CREATE_FOLDER_TASK,
    CREATE_TEXT_FILE_TASK,
    EMPTY_TRASH_TASK,
    PREVIEW_TRASH_TASK,
    run_storage_operation,
)
from vault_shared import NotFoundError, ValidationError
from vault_shared.ai_gateway.recommendation import validate_component_name
from vault_shared.db.repositories import (
    AuditLogRepository,
    FolderRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)

EMPTY_TRASH_CONFIRMATION = "EMPTY TRASH"
_EMPTY_TRASH_WAIT_SECONDS = 180
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
        self._audit_logs = AuditLogRepository(db)

    def create_folder(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        parent_folder_id: uuid.UUID | None,
    ) -> dict:
        self._check_owned(connector_id, organization_id=organization_id, folder_id=parent_folder_id)
        result = run_storage_operation(
            CREATE_FOLDER_TASK,
            args=[
                str(organization_id),
                str(connector_id),
                _valid_name(name),
                str(parent_folder_id) if parent_folder_id else None,
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
            ],
        )
        return self._finish(
            result, organization_id=organization_id, user_id=user_id, event="file_created"
        )

    def preview_trash(self, connector_id: uuid.UUID, *, organization_id: uuid.UUID) -> dict:
        self._check_owned(connector_id, organization_id=organization_id, folder_id=None)
        result = run_storage_operation(
            PREVIEW_TRASH_TASK, args=[str(organization_id), str(connector_id)]
        )
        if not result.get("ok"):
            raise ValidationError(str(result.get("error") or "Couldn't read Drive's Trash."))
        return result

    def empty_trash(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        expected_count: int,
        confirmation: str,
    ) -> dict:
        """Irreversible: the typed confirmation and the reviewed file count
        are both required, so a stray click or a stale screen can't delete
        anything."""
        typed = "".join(ch for ch in confirmation.upper() if ch.isalpha())
        if typed != EMPTY_TRASH_CONFIRMATION.replace(" ", ""):
            raise ValidationError(f"Type {EMPTY_TRASH_CONFIRMATION} to confirm.")
        self._check_owned(connector_id, organization_id=organization_id, folder_id=None)
        result = run_storage_operation(
            EMPTY_TRASH_TASK,
            args=[str(organization_id), str(connector_id), expected_count],
            wait_seconds=_EMPTY_TRASH_WAIT_SECONDS,
        )
        if not result.get("ok"):
            raise ValidationError(str(result.get("error") or "Couldn't empty Drive's Trash."))
        self._audit_logs.record(
            event_type="trash_emptied",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"file_count": result["file_count"], "total_bytes": result["total_bytes"]},
        )
        self._db.commit()
        return result

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


def _valid_name(name: str) -> str:
    try:
        return validate_component_name(name.strip(), what="Name")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
