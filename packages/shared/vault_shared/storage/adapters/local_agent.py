"""`StorageAdapter` for folders and drives on a user's own machine.

AI Vault cannot reach into the machine, so every call becomes a typed command
queued for the AI Vault Local Agent running there. The agent runs it from its
fixed table of operations (never a shell), reads the disk back to confirm the
result, and posts it; this adapter waits for that confirmed result. Items are
addressed by the agent's stable file identity (device and file id), which
survives renames, moves and the agent's Trash.
"""

import base64
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared.db.models import AgentCommandStatus, StorageConnector
from vault_shared.db.repositories import (
    AgentCommandRepository,
    LocalAgentRepository,
    StorageSourceRepository,
)
from vault_shared.storage.capabilities import StorageCapabilities
from vault_shared.storage.errors import (
    StorageError,
    StorageForbiddenError,
    StorageNotFoundError,
    StorageUnavailableError,
    StorageUnsupportedError,
)
from vault_shared.storage.models import (
    FOLDER_MIME_TYPE,
    ByteStream,
    ChangePage,
    ConnectionHealth,
    ContainerKind,
    ExportFormat,
    ExportPurpose,
    FilePage,
    HealthStatus,
    ProviderFileId,
    Revision,
    StorageContainer,
    StorageFile,
    StoragePermission,
    StorageQuota,
)

PROVIDER = "local_agent"
# An agent that hasn't reported in for this long is treated as offline.
_ONLINE_WINDOW = timedelta(seconds=90)
_POLL_SECONDS = 0.4
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_CREATED_FILE_BYTES = 1024 * 1024


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def to_storage_file(data: dict, *, connection_id: uuid.UUID | None = None) -> StorageFile:
    return StorageFile(
        provider=PROVIDER,
        provider_file_id=ProviderFileId(data["id"]),
        name=data["name"],
        mime_type=FOLDER_MIME_TYPE
        if data.get("is_dir")
        else (data.get("mime_type") or "application/octet-stream"),
        is_folder=bool(data.get("is_dir")),
        trashed=bool(data.get("trashed")),
        connection_id=connection_id,
        size_bytes=None if data.get("is_dir") else data.get("size_bytes"),
        parent_id=ProviderFileId(data["parent_id"]) if data.get("parent_id") else None,
        parent_ids=(ProviderFileId(data["parent_id"]),) if data.get("parent_id") else (),
        path="/" + data["relative_path"] if data.get("relative_path") else None,
        created_at=_parse_time(data.get("created_at")),
        modified_at=_parse_time(data.get("modified_at")),
        accessed_at=_parse_time(data.get("accessed_at")),
        owner_email=None,
        checksum=data.get("sha256"),
        revision=Revision(
            token=data.get("sha256"), modified_at=_parse_time(data.get("modified_at"))
        ),
    )


class LocalAgentAdapter:
    provider = PROVIDER

    def __init__(
        self,
        *,
        connector: StorageConnector,
        session_factory: Callable[[], Session],
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._connector_id = connector.id
        self._session_factory = session_factory
        self._timeout = timeout_seconds

    # -- connection ------------------------------------------------------------

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            supports_trash=True,
            supports_restore=True,
            supports_permanent_delete=True,
            supports_copy=True,
            supports_create_folder=True,
            supports_upload=True,
            supports_download=True,
            supports_hash=True,
        )

    def connect(self) -> None:
        with self._session_factory() as session:
            agent = LocalAgentRepository(session).get_by_connector_id(self._connector_id)
            if agent is None or not _online(agent.last_seen_at):
                name = agent.device_name if agent and agent.device_name else "this computer"
                raise StorageUnavailableError(
                    f"The AI Vault agent on {name} is offline — start it to work with these files.",
                    provider=PROVIDER,
                )

    def health(self) -> ConnectionHealth:
        try:
            self.connect()
        except StorageUnavailableError as error:
            return ConnectionHealth(status=HealthStatus.DEGRADED, detail=error.message)
        return ConnectionHealth(status=HealthStatus.OK)

    def disconnect(self) -> None:
        return None

    def write_access_problems(self) -> list[str]:
        return []

    # -- discovery -------------------------------------------------------------

    def list_containers(self) -> list[StorageContainer]:
        with self._session_factory() as session:
            sources = StorageSourceRepository(session).list_for_connector(self._connector_id)
            return [
                StorageContainer(
                    id=source.provider_drive_id, name=source.name, kind=ContainerKind.PERSONAL
                )
                for source in sources
            ]

    def scan(self, container_id: str, cursor: str | None = None) -> FilePage:
        raise StorageUnsupportedError("The Local Agent sends its own scans.", provider=PROVIDER)

    def get_change_cursor(self, container_id: str) -> str:
        raise StorageUnsupportedError("The Local Agent sends its own scans.", provider=PROVIDER)

    def check_changes(self, container_id: str, cursor: str) -> ChangePage:
        raise StorageUnsupportedError("The Local Agent sends its own scans.", provider=PROVIDER)

    def get_file(self, file_id: ProviderFileId) -> StorageFile:
        return self._file(self._call("get", {"id": file_id}))

    def get_permissions(self, file_id: ProviderFileId) -> list[StoragePermission]:
        return []

    # -- reading ---------------------------------------------------------------

    def open_read(self, file_id: ProviderFileId) -> ByteStream:
        result = self._call("read", {"id": file_id})
        return iter([base64.b64decode(result["content_base64"])])

    def export(self, file_id: ProviderFileId, export_format: ExportFormat) -> ByteStream:
        raise StorageUnsupportedError("Local files have no export formats.", provider=PROVIDER)

    def export_format_for(self, mime_type: str, purpose: ExportPurpose) -> ExportFormat | None:
        return None

    def is_native_document(self, mime_type: str) -> bool:
        return False

    def get_thumbnail(self, file_id: ProviderFileId) -> bytes | None:
        return None

    def storage_quota(self) -> StorageQuota:
        with self._session_factory() as session:
            agent = LocalAgentRepository(session).get_by_connector_id(self._connector_id)
            volumes = agent.volumes if agent else []
        return StorageQuota(
            used_bytes=sum(v.get("used_bytes", 0) for v in volumes) if volumes else None,
            total_bytes=sum(v.get("total_bytes", 0) for v in volumes) if volumes else None,
        )

    def list_trash(self) -> list[StorageFile]:
        result = self._call("trash_contents", {})
        return [to_storage_file(item, connection_id=self._connector_id) for item in result["files"]]

    # -- mutation (Execution Engine only) ----------------------------------------

    def rename(self, file_id: ProviderFileId, new_name: str) -> StorageFile:
        return self._file(self._call("rename", {"id": file_id, "new_name": new_name}))

    def move(
        self,
        file_id: ProviderFileId,
        *,
        new_parent_id: ProviderFileId,
        old_parent_id: ProviderFileId,
    ) -> StorageFile:
        return self._file(self._call("move", {"id": file_id, "parent_id": new_parent_id}))

    def copy(
        self,
        file_id: ProviderFileId,
        *,
        new_name: str | None = None,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        params: dict = {"id": file_id}
        if new_name:
            params["new_name"] = new_name
        if parent_id:
            params["parent_id"] = parent_id
        return self._file(self._call("copy", params))

    def create_folder(self, name: str, *, parent_id: ProviderFileId | None = None) -> StorageFile:
        return self._file(self._call("create_folder", {"name": name, **self._parent(parent_id)}))

    def trash(self, file_id: ProviderFileId) -> StorageFile:
        return self._file(self._call("trash", {"id": file_id}))

    def restore(self, file_id: ProviderFileId) -> StorageFile:
        return self._file(self._call("restore", {"id": file_id}))

    def permanent_delete(self, file_id: ProviderFileId) -> None:
        self._call("permanent_delete", {"id": file_id, "confirm": True})

    def empty_trash(self) -> None:
        self._call("empty_trash", {"confirm": True})

    def update_metadata(self, file_id: ProviderFileId, properties: dict[str, str]) -> StorageFile:
        raise StorageUnsupportedError("Local files have no app properties.", provider=PROVIDER)

    def upload(
        self,
        name: str,
        content: ByteStream,
        *,
        mime_type: str,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        data = b"".join(content)
        if len(data) > _MAX_CREATED_FILE_BYTES:
            raise StorageUnsupportedError(
                "Only small text files can be created on this computer so far.", provider=PROVIDER
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise StorageUnsupportedError(
                "Only text files can be created on this computer so far.", provider=PROVIDER
            ) from error
        return self._file(
            self._call("create_file", {"name": name, "content": text, **self._parent(parent_id)})
        )

    # -- the command round trip --------------------------------------------------

    def _call(self, op: str, params: dict) -> dict:
        with self._session_factory() as session:
            command = AgentCommandRepository(session).enqueue(
                connector_id=self._connector_id, op=op, params=params
            )
            session.commit()
            command_id = command.id
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            with self._session_factory() as session:
                finished = AgentCommandRepository(session).get(command_id)
                if finished is not None and finished.status in (
                    AgentCommandStatus.DONE,
                    AgentCommandStatus.FAILED,
                ):
                    result = dict(finished.result or {})
                    break
        else:
            raise StorageUnavailableError(
                "The AI Vault agent didn't answer in time — is it running?", provider=PROVIDER
            )
        if result.get("ok"):
            return result
        message = str(result.get("error") or "The agent could not do that.")
        kind = result.get("kind")
        if kind == "not_found":
            raise StorageNotFoundError(message, provider=PROVIDER)
        if kind == "refused":
            raise StorageForbiddenError(message, provider=PROVIDER)
        raise StorageError(message, provider=PROVIDER)

    def _file(self, result: dict) -> StorageFile:
        return to_storage_file(result["file"], connection_id=self._connector_id)

    def _parent(self, parent_id: ProviderFileId | None) -> dict:
        if parent_id:
            return {"parent_id": parent_id}
        with self._session_factory() as session:
            sources = StorageSourceRepository(session).list_for_connector(self._connector_id)
            agent = LocalAgentRepository(session).get_by_connector_id(self._connector_id)
            roots = agent.roots if agent else []
        if len(sources) == 1 and len(roots) == 1:
            return {"root": roots[0]}
        raise StorageUnsupportedError(
            "Choose which folder on this computer to create it in.", provider=PROVIDER
        )


def _online(last_seen_at: datetime | None) -> bool:
    return last_seen_at is not None and datetime.now(UTC) - last_seen_at < _ONLINE_WINDOW
