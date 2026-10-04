"""Shared test support for the storage layer.

* `FakeDriveApi` — an in-memory stand-in for `GoogleDriveClient` with the same
  methods, signatures and error behavior, so the *real* `GoogleDriveAdapter`
  runs in tests without Google credentials.
* `InMemoryStorageAdapter` — a second, independent `StorageAdapter`
  implementation that shares no code with the Google one. It exists so the
  contract suite can prove the contract is not Google-shaped, and it is the
  fake to inject when a service test needs an adapter.
* Harnesses (`GoogleDriveHarness`, `InMemoryHarness`) expose one uniform set
  of "the outside world did X" operations to `storage_contract`, so any future
  adapter only has to provide a harness to inherit the whole suite.

Nothing here talks to a network or a real provider.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from vault_shared.connectors.google_drive import (
    DriveChangesPage,
    DriveFile,
    DriveFilesPage,
    DrivePermission,
    SharedDrive,
)
from vault_shared.connectors.google_workspace import DRIVE_WRITE_SCOPE
from vault_shared.errors import ReauthRequiredError
from vault_shared.storage import (
    FOLDER_MIME_TYPE,
    ByteStream,
    Capability,
    ChangePage,
    ConnectionHealth,
    ContainerKind,
    ExportFormat,
    ExportPurpose,
    FilePage,
    HealthStatus,
    ProviderFileId,
    Revision,
    StorageAuthenticationRequiredError,
    StorageCapabilities,
    StorageConflictError,
    StorageContainer,
    StorageFile,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StoragePermission,
    StorageRateLimitedError,
    StorageUnauthorizedError,
    StorageUnavailableError,
)
from vault_shared.storage.adapters.google_drive import GoogleDriveAdapter

SECRET_TOKEN = "SECRET-ACCESS-TOKEN-4f9c"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class StaticCredentials:
    """A `CredentialSource` with switches for the failure modes tests need."""

    def __init__(self, *, token: str = SECRET_TOKEN, scopes: tuple[str, ...] | None = None) -> None:
        self.token = token
        self.scopes: tuple[str, ...] | None = (DRIVE_WRITE_SCOPE,) if scopes is None else scopes
        self.token_error: Exception | None = None
        self.token_requests = 0
        self.revoked = False

    def access_token(self) -> str:
        self.token_requests += 1
        if self.token_error is not None:
            raise self.token_error
        return self.token

    def granted_scopes(self) -> tuple[str, ...] | None:
        return self.scopes

    def revoke(self) -> None:
        self.revoked = True


def google_adapter(
    client: object, *, credentials: StaticCredentials | None = None
) -> GoogleDriveAdapter:
    return GoogleDriveAdapter(
        client=client,  # type: ignore[arg-type]
        credentials=credentials or StaticCredentials(),
        connection_id=uuid.uuid4(),
    )


def failure_for(kind: str) -> Exception:
    """The error a provider-facing layer raises for each failure kind."""
    errors: dict[str, Exception] = {
        "unauthorized": StorageUnauthorizedError("rejected", provider="google_workspace"),
        "rate_limited": StorageRateLimitedError(
            "slow down", provider="google_workspace", retry_after_seconds=7.0
        ),
        "unavailable": StorageUnavailableError("down", provider="google_workspace"),
        "timeout": StorageUnavailableError(
            "timed out", provider="google_workspace", provider_code="timeout"
        ),
        "forbidden": StorageForbiddenError("nope", provider="google_workspace"),
        "conflict": StorageConflictError("clash", provider="google_workspace"),
        "invalid": StorageInvalidRequestError("bad", provider="google_workspace"),
    }
    return errors[kind]


class _Stream:
    """A closeable chunk iterator that records whether it was closed."""

    def __init__(self, data: bytes, chunk_size: int) -> None:
        self._chunks = iter(
            [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
        )
        self.closed = False

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> bytes:
        if self.closed:
            raise StopIteration
        return next(self._chunks)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fake Google Drive HTTP client
# ---------------------------------------------------------------------------


class FakeDriveApi:
    """Same surface and error behavior as `GoogleDriveClient`, in memory."""

    def __init__(
        self, *, page_size: int = 1000, change_page_size: int = 1000, chunk_size: int = 4
    ) -> None:
        self.valid_token = SECRET_TOKEN
        self.page_size = page_size
        self.change_page_size = change_page_size
        self.chunk_size = chunk_size
        self.files: dict[str, DriveFile] = {}
        self.content: dict[str, bytes] = {}
        self.container_of: dict[str, str] = {}
        self.changes: list[tuple[str, str]] = []
        self.shared_drives: list[SharedDrive] = []
        self.permissions: dict[str, list[DrivePermission]] = {}
        self.thumbnail: bytes | None = b"thumbnail-bytes"
        self.account_email = "owner@example.com"
        self.calls: list[str] = []
        self.streams: list[_Stream] = []
        self._failures: list[Exception] = []
        self._clock = 0
        self._revision = 0

    # -- test controls ------------------------------------------------------

    def fail_next(self, error: Exception) -> None:
        self._failures.append(error)

    def calls_to(self, method: str) -> int:
        return self.calls.count(method)

    def seed(
        self,
        name: str,
        *,
        parent: str = "root-folder",
        content: bytes = b"hello world",
        mime_type: str = "text/plain",
        container: str = "root",
        is_folder: bool = False,
    ) -> DriveFile:
        file_id = f"drv-{uuid.uuid4().hex[:10]}"
        item = self._new(file_id, name, mime_type, [parent], len(content), is_folder)
        self.files[file_id] = item
        self.content[file_id] = content
        self.container_of[file_id] = container
        self.changes.append(("changed", file_id))
        return item

    def external_edit(self, file_id: str) -> None:
        item = self.files[file_id]
        self._revision += 1
        self.files[file_id] = replace(
            item, version_id=f"rev-{self._revision}", modified_time=self._tick()
        )
        self.changes.append(("changed", file_id))

    def external_rename(self, file_id: str, name: str) -> None:
        self.files[file_id] = replace(self.files[file_id], name=name, modified_time=self._tick())
        self.changes.append(("changed", file_id))

    def external_trash(self, file_id: str) -> None:
        self.files[file_id] = replace(self.files[file_id], trashed=True)
        self.changes.append(("changed", file_id))

    def external_delete(self, file_id: str) -> None:
        self.files.pop(file_id)
        self.changes.append(("removed", file_id))

    # -- internals ----------------------------------------------------------

    def _tick(self) -> datetime:
        self._clock += 1
        return _EPOCH + timedelta(seconds=self._clock)

    def _new(
        self, file_id: str, name: str, mime: str, parents: list[str], size: int, is_folder: bool
    ) -> DriveFile:
        self._revision += 1
        return DriveFile(
            id=file_id,
            name=name,
            mime_type=FOLDER_MIME_TYPE if is_folder else mime,
            parents=parents,
            size=None if is_folder else size,
            created_time=self._tick(),
            modified_time=self._tick(),
            viewed_by_me_time=None,
            owner_email="owner@example.com",
            shared=False,
            checksum=None if is_folder else f"md5-{file_id}",
            version_id=f"rev-{self._revision}",
            is_folder=is_folder,
            trashed=False,
            web_view_link=f"https://drive.example/{file_id}",
        )

    def _enter(self, method: str, access_token: str) -> None:
        self.calls.append(method)
        if self._failures:
            raise self._failures.pop(0)
        if access_token != self.valid_token:
            raise StorageUnauthorizedError(
                "Google Drive rejected the access token.", provider="google_workspace"
            )

    def _get(self, file_id: str) -> DriveFile:
        item = self.files.get(file_id)
        if item is None:
            raise StorageNotFoundError("Google Drive item not found.", provider="google_workspace")
        return item

    def _put(self, item: DriveFile) -> DriveFile:
        self.files[item.id] = replace(item, modified_time=self._tick())
        self.changes.append(("changed", item.id))
        return self.files[item.id]

    def _check_parent(self, parent_id: str) -> None:
        if parent_id != "root-folder" and parent_id not in self.files:
            raise StorageNotFoundError("Google Drive item not found.", provider="google_workspace")

    # -- GoogleDriveClient surface -------------------------------------------

    def list_shared_drives(self, *, access_token: str) -> list[SharedDrive]:
        self._enter("list_shared_drives", access_token)
        return list(self.shared_drives)

    def list_files_page(
        self,
        *,
        access_token: str,
        drive_id: str | None,
        page_token: str | None,
        page_size: int = 1000,
    ) -> DriveFilesPage:
        self._enter("list_files_page", access_token)
        container = drive_id or "root"
        visible = [
            item
            for file_id, item in self.files.items()
            if self.container_of.get(file_id) == container and not item.trashed
        ]
        start = int(page_token or 0)
        limit = min(page_size, self.page_size)
        chunk = visible[start : start + limit]
        more = start + limit < len(visible)
        return DriveFilesPage(files=chunk, next_page_token=str(start + limit) if more else None)

    def get_start_page_token(self, *, access_token: str, drive_id: str | None) -> str:
        self._enter("get_start_page_token", access_token)
        return str(len(self.changes))

    def list_changes_page(
        self, *, access_token: str, page_token: str, drive_id: str | None
    ) -> DriveChangesPage:
        self._enter("list_changes_page", access_token)
        start = int(page_token)
        window = self.changes[start : start + self.change_page_size]
        more = start + self.change_page_size < len(self.changes)
        changed = [self.files[i] for kind, i in window if kind == "changed" and i in self.files]
        removed = [i for kind, i in window if kind == "removed"]
        return DriveChangesPage(
            changed_files=changed,
            removed_file_ids=removed,
            next_page_token=str(start + self.change_page_size) if more else None,
            new_start_page_token=None if more else str(len(self.changes)),
        )

    def get_file(self, *, access_token: str, file_id: str) -> DriveFile:
        self._enter("get_file", access_token)
        return self._get(file_id)

    def download_file(self, *, access_token: str, file_id: str) -> bytes:
        self._enter("download_file", access_token)
        self._get(file_id)
        return self.content.get(file_id, b"")

    def export_file(self, *, access_token: str, file_id: str, export_mime_type: str) -> bytes:
        self._enter("export_file", access_token)
        self._get(file_id)
        return self.content.get(file_id, b"")

    def stream_file(self, *, access_token: str, file_id: str) -> ByteStream:
        self._enter("stream_file", access_token)
        self._get(file_id)
        stream = _Stream(self.content.get(file_id, b""), self.chunk_size)
        self.streams.append(stream)
        return stream

    def stream_export(
        self, *, access_token: str, file_id: str, export_mime_type: str
    ) -> ByteStream:
        self._enter("stream_export", access_token)
        self._get(file_id)
        stream = _Stream(self.content.get(file_id, b""), self.chunk_size)
        self.streams.append(stream)
        return stream

    def rename_file(self, *, access_token: str, file_id: str, new_name: str) -> DriveFile:
        self._enter("rename_file", access_token)
        return self._put(replace(self._get(file_id), name=new_name))

    def move_file(
        self, *, access_token: str, file_id: str, add_parent_id: str, remove_parent_id: str
    ) -> DriveFile:
        self._enter("move_file", access_token)
        item = self._get(file_id)
        self._check_parent(add_parent_id)
        parents = [p for p in item.parents if p != remove_parent_id] + [add_parent_id]
        return self._put(replace(item, parents=parents))

    def set_trashed(self, *, access_token: str, file_id: str, trashed: bool) -> DriveFile:
        self._enter("set_trashed", access_token)
        return self._put(replace(self._get(file_id), trashed=trashed))

    def delete_file(self, *, access_token: str, file_id: str) -> None:
        self._enter("delete_file", access_token)
        self._get(file_id)
        del self.files[file_id]
        self.changes.append(("removed", file_id))

    def update_app_properties(
        self, *, access_token: str, file_id: str, properties: dict[str, str]
    ) -> DriveFile:
        self._enter("update_app_properties", access_token)
        return self._put(self._get(file_id))

    def copy_file(
        self, *, access_token: str, file_id: str, new_name: str | None, parent_id: str | None
    ) -> DriveFile:
        self._enter("copy_file", access_token)
        source = self._get(file_id)
        if parent_id is not None:
            self._check_parent(parent_id)
        copy_id = f"drv-{uuid.uuid4().hex[:10]}"
        copied = self._new(
            copy_id,
            new_name or f"Copy of {source.name}",
            source.mime_type,
            [parent_id] if parent_id else list(source.parents),
            source.size or 0,
            source.is_folder,
        )
        self.files[copy_id] = copied
        self.content[copy_id] = self.content.get(file_id, b"")
        self.container_of[copy_id] = self.container_of.get(file_id, "root")
        self.changes.append(("changed", copy_id))
        return copied

    def create_folder(self, *, access_token: str, name: str, parent_id: str | None) -> DriveFile:
        self._enter("create_folder", access_token)
        parent = parent_id or "root-folder"
        self._check_parent(parent)
        folder_id = f"drv-{uuid.uuid4().hex[:10]}"
        folder = self._new(folder_id, name, FOLDER_MIME_TYPE, [parent], 0, True)
        self.files[folder_id] = folder
        self.container_of[folder_id] = "root"
        self.changes.append(("changed", folder_id))
        return folder

    def list_permissions(self, *, access_token: str, file_id: str) -> list[DrivePermission]:
        self._enter("list_permissions", access_token)
        self._get(file_id)
        return list(self.permissions.get(file_id, []))

    def get_thumbnail(self, *, access_token: str, file_id: str) -> bytes | None:
        self._enter("get_thumbnail", access_token)
        self._get(file_id)
        return self.thumbnail

    def get_account_email(self, *, access_token: str) -> str | None:
        self._enter("get_account_email", access_token)
        return self.account_email


# ---------------------------------------------------------------------------
# A second, independent adapter implementation
# ---------------------------------------------------------------------------


@dataclass
class _Item:
    file: StorageFile
    content: bytes = b""
    container: str = "vol-1"


class InMemoryStorageAdapter:
    """A `StorageAdapter` that keeps everything in dictionaries.

    Deliberately unlike the Google adapter — path-free ids, its own revision
    scheme, its own change log, capabilities you can switch off — so passing
    the same contract suite shows the contract is provider-neutral. Also the
    fake to inject into service tests.
    """

    provider = "in_memory"
    ROOT = "vol-root"

    def __init__(self, *, capabilities: StorageCapabilities | None = None) -> None:
        self._capabilities = capabilities or StorageCapabilities(
            supports_trash=True,
            supports_restore=True,
            supports_permanent_delete=True,
            supports_copy=True,
            supports_create_folder=True,
            supports_upload=True,
            supports_download=True,
            supports_change_feed=True,
            supports_native_export=True,
            supports_streaming=True,
            supports_hash=True,
            supports_metadata_update=True,
        )
        self.items: dict[str, _Item] = {}
        self.log: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.page_size = 1000
        self.write_access = True
        self.credentials_valid = True
        self.disconnected = False
        self.pending_failure: Exception | None = None
        self._counter = 0
        self._clock = 0

    # -- helpers ------------------------------------------------------------

    def _enter(self, method: str) -> None:
        self.calls.append(method)
        if self.pending_failure is not None:
            error, self.pending_failure = self.pending_failure, None
            raise error
        if not self.credentials_valid:
            raise StorageAuthenticationRequiredError("reconnect", provider=self.provider)

    def _tick(self) -> datetime:
        self._clock += 1
        return _EPOCH + timedelta(seconds=self._clock)

    def _next_id(self) -> str:
        self._counter += 1
        return f"mem-{self._counter}"

    def _get(self, file_id: str) -> _Item:
        item = self.items.get(file_id)
        if item is None:
            raise StorageNotFoundError("not found", provider=self.provider)
        return item

    def _store(self, item: _Item, **changes: object) -> StorageFile:
        updated = replace(item.file, **changes, modified_at=self._tick())  # type: ignore[arg-type]
        item.file = replace(
            updated,
            revision=Revision(token=updated.revision.token, modified_at=updated.modified_at),
        )
        self.log.append(("changed", item.file.provider_file_id))
        return item.file

    def _require(self, capability: Capability) -> None:
        self._capabilities.require(capability, provider=self.provider)

    def add(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        content: bytes = b"hello world",
        mime_type: str = "text/plain",
        is_folder: bool = False,
    ) -> StorageFile:
        file_id = self._next_id()
        parent = ProviderFileId(parent_id or self.ROOT)
        now = self._tick()
        stored = StorageFile(
            provider=self.provider,
            provider_file_id=ProviderFileId(file_id),
            name=name,
            mime_type=FOLDER_MIME_TYPE if is_folder else mime_type,
            is_folder=is_folder,
            size_bytes=None if is_folder else len(content),
            parent_id=parent,
            parent_ids=(parent,),
            created_at=now,
            modified_at=now,
            checksum=None if is_folder else f"sha-{file_id}",
            revision=Revision(token=f"v1-{file_id}", modified_at=now),
            native_document=self.is_native_document(mime_type),
        )
        self.items[file_id] = _Item(stored, content)
        self.log.append(("changed", file_id))
        return stored

    def bump_revision(self, file_id: str) -> None:
        item = self._get(file_id)
        self._store(
            item, revision=Revision(token=f"v{self._tick().second}-{file_id}", modified_at=None)
        )

    # -- StorageAdapter ------------------------------------------------------

    def capabilities(self) -> StorageCapabilities:
        return self._capabilities

    def connect(self) -> None:
        self._enter("connect")

    def health(self) -> ConnectionHealth:
        try:
            self._enter("health")
        except StorageAuthenticationRequiredError:
            return ConnectionHealth(HealthStatus.AUTHENTICATION_REQUIRED, detail="reconnect")
        except (StorageUnavailableError, StorageRateLimitedError) as exc:
            return ConnectionHealth(HealthStatus.DEGRADED, detail=exc.message)
        return ConnectionHealth(HealthStatus.OK, account="mem-user")

    def disconnect(self) -> None:
        self.disconnected = True

    def write_access_problems(self) -> list[str]:
        return [] if self.write_access else ["Read-only grant."]

    def list_containers(self) -> list[StorageContainer]:
        self._enter("list_containers")
        return [
            StorageContainer("vol-1", "Main volume", ContainerKind.PERSONAL),
            StorageContainer("vol-2", "Team volume", ContainerKind.SHARED),
        ]

    def scan(self, container_id: str, cursor: str | None = None) -> FilePage:
        self._enter("scan")
        visible = [
            i.file
            for i in self.items.values()
            if i.container == container_id and not i.file.trashed
        ]
        start = int(cursor or 0)
        chunk = visible[start : start + self.page_size]
        more = start + self.page_size < len(visible)
        return FilePage(tuple(chunk), str(start + self.page_size) if more else None)

    def get_change_cursor(self, container_id: str) -> str:
        self._enter("get_change_cursor")
        self._require(Capability.CHANGE_FEED)
        return str(len(self.log))

    def check_changes(self, container_id: str, cursor: str) -> ChangePage:
        self._enter("check_changes")
        self._require(Capability.CHANGE_FEED)
        window = self.log[int(cursor) :]
        changed = tuple(
            self.items[i].file for kind, i in window if kind == "changed" and i in self.items
        )
        removed = tuple(ProviderFileId(i) for kind, i in window if kind == "removed")
        return ChangePage(changed, removed, None, str(len(self.log)))

    def get_file(self, file_id: ProviderFileId) -> StorageFile:
        self._enter("get_file")
        return self._get(file_id).file

    def get_permissions(self, file_id: ProviderFileId) -> list[StoragePermission]:
        self._enter("get_permissions")
        self._require(Capability.PERMISSIONS)
        self._get(file_id)
        return [StoragePermission("user", "owner", "mem-user")]

    def open_read(self, file_id: ProviderFileId) -> ByteStream:
        self._enter("open_read")
        self._require(Capability.DOWNLOAD)
        return _Stream(self._get(file_id).content, 3)

    def export(self, file_id: ProviderFileId, export_format: ExportFormat) -> ByteStream:
        self._enter("export")
        self._require(Capability.NATIVE_EXPORT)
        return _Stream(self._get(file_id).content, 3)

    def export_format_for(self, mime_type: str, purpose: ExportPurpose) -> ExportFormat | None:
        if mime_type != "application/x-native-doc" or not self._capabilities.supports_native_export:
            return None
        return ExportFormat("text/plain", "txt", "mem_doc_export")

    def is_native_document(self, mime_type: str) -> bool:
        return mime_type == "application/x-native-doc"

    def get_thumbnail(self, file_id: ProviderFileId) -> bytes | None:
        self._enter("get_thumbnail")
        self._require(Capability.THUMBNAILS)
        self._get(file_id)
        return b"thumb"

    def rename(self, file_id: ProviderFileId, new_name: str) -> StorageFile:
        self._enter("rename")
        return self._store(self._get(file_id), name=new_name)

    def move(
        self,
        file_id: ProviderFileId,
        *,
        new_parent_id: ProviderFileId,
        old_parent_id: ProviderFileId,
    ) -> StorageFile:
        self._enter("move")
        item = self._get(file_id)
        if new_parent_id != self.ROOT and new_parent_id not in self.items:
            raise StorageNotFoundError("destination not found", provider=self.provider)
        parents = tuple(p for p in item.file.parent_ids if p != old_parent_id) + (new_parent_id,)
        return self._store(item, parent_id=new_parent_id, parent_ids=parents)

    def copy(
        self,
        file_id: ProviderFileId,
        *,
        new_name: str | None = None,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        self._enter("copy")
        self._require(Capability.COPY)
        source = self._get(file_id)
        return self.add(
            new_name or f"Copy of {source.file.name}",
            parent_id=parent_id or source.file.parent_id,
            content=source.content,
            mime_type=source.file.mime_type,
        )

    def create_folder(self, name: str, *, parent_id: ProviderFileId | None = None) -> StorageFile:
        self._enter("create_folder")
        self._require(Capability.CREATE_FOLDER)
        return self.add(name, parent_id=parent_id, is_folder=True)

    def trash(self, file_id: ProviderFileId) -> StorageFile:
        self._enter("trash")
        self._require(Capability.TRASH)
        return self._store(self._get(file_id), trashed=True)

    def restore(self, file_id: ProviderFileId) -> StorageFile:
        self._enter("restore")
        self._require(Capability.RESTORE)
        return self._store(self._get(file_id), trashed=False)

    def permanent_delete(self, file_id: ProviderFileId) -> None:
        self._enter("permanent_delete")
        self._require(Capability.PERMANENT_DELETE)
        self._get(file_id)
        del self.items[file_id]
        self.log.append(("removed", file_id))

    def update_metadata(self, file_id: ProviderFileId, properties: dict[str, str]) -> StorageFile:
        self._enter("update_metadata")
        self._require(Capability.METADATA_UPDATE)
        return self._store(self._get(file_id))

    def upload(
        self,
        name: str,
        content: ByteStream,
        *,
        mime_type: str,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        self._enter("upload")
        self._require(Capability.UPLOAD)
        return self.add(name, parent_id=parent_id, content=b"".join(content), mime_type=mime_type)


# ---------------------------------------------------------------------------
# Harnesses — one uniform "outside world" for the contract suite
# ---------------------------------------------------------------------------


@dataclass
class GoogleDriveHarness:
    api: FakeDriveApi = field(default_factory=FakeDriveApi)
    credentials: StaticCredentials = field(default_factory=StaticCredentials)
    container_id: str = "root"
    root_id: str = "root-folder"
    secret: str = SECRET_TOKEN
    provider: str = "google_workspace"

    def __post_init__(self) -> None:
        self.adapter = google_adapter(self.api, credentials=self.credentials)

    def seed_file(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        content: bytes = b"hello world",
        mime_type: str = "text/plain",
    ) -> ProviderFileId:
        item = self.api.seed(
            name, parent=parent_id or self.root_id, content=content, mime_type=mime_type
        )
        return ProviderFileId(item.id)

    def seed_folder(self, name: str, *, parent_id: str | None = None) -> ProviderFileId:
        item = self.api.seed(name, parent=parent_id or self.root_id, is_folder=True)
        return ProviderFileId(item.id)

    def seed_native_document(self, name: str) -> tuple[ProviderFileId, str]:
        item = self.api.seed(name, mime_type=GOOGLE_DOC_MIME, content=b"doc text")
        return ProviderFileId(item.id), GOOGLE_DOC_MIME

    def external_edit(self, file_id: str) -> None:
        self.api.external_edit(file_id)

    def external_rename(self, file_id: str, name: str) -> None:
        self.api.external_rename(file_id, name)

    def external_trash(self, file_id: str) -> None:
        self.api.external_trash(file_id)

    def external_delete(self, file_id: str) -> None:
        self.api.external_delete(file_id)

    def set_page_size(self, size: int) -> None:
        self.api.page_size = size
        self.api.change_page_size = size

    def revoke_credentials(self) -> None:
        self.credentials.token_error = ReauthRequiredError("refresh token revoked")

    def rotate_token_provider_side(self) -> None:
        """The access token in use stops being accepted mid-flight."""
        self.api.valid_token = "a-different-token"

    def make_read_only(self) -> None:
        self.credentials.scopes = ("https://www.googleapis.com/auth/drive.readonly",)

    def inject_failure(self, kind: str) -> None:
        self.api.fail_next(failure_for(kind))

    def disconnected(self) -> bool:
        return self.credentials.revoked

    def provider_calls(self) -> list[str]:
        return self.api.calls


class InMemoryHarness:
    provider = "in_memory"
    container_id = "vol-1"
    root_id = InMemoryStorageAdapter.ROOT
    secret = "mem-secret"

    def __init__(self, capabilities: StorageCapabilities | None = None) -> None:
        self.memory = InMemoryStorageAdapter(capabilities=capabilities)
        self.adapter: InMemoryStorageAdapter = self.memory

    def seed_file(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        content: bytes = b"hello world",
        mime_type: str = "text/plain",
    ) -> ProviderFileId:
        return self.memory.add(
            name, parent_id=parent_id, content=content, mime_type=mime_type
        ).provider_file_id

    def seed_folder(self, name: str, *, parent_id: str | None = None) -> ProviderFileId:
        return self.memory.add(name, parent_id=parent_id, is_folder=True).provider_file_id

    def seed_native_document(self, name: str) -> tuple[ProviderFileId, str]:
        mime = "application/x-native-doc"
        return self.memory.add(name, mime_type=mime, content=b"doc text").provider_file_id, mime

    def external_edit(self, file_id: str) -> None:
        self.memory.bump_revision(file_id)

    def external_rename(self, file_id: str, name: str) -> None:
        self.memory._store(self.memory._get(file_id), name=name)

    def external_trash(self, file_id: str) -> None:
        self.memory._store(self.memory._get(file_id), trashed=True)

    def external_delete(self, file_id: str) -> None:
        del self.memory.items[file_id]
        self.memory.log.append(("removed", file_id))

    def set_page_size(self, size: int) -> None:
        self.memory.page_size = size

    def revoke_credentials(self) -> None:
        self.memory.credentials_valid = False

    def rotate_token_provider_side(self) -> None:
        self.memory.pending_failure = failure_for("unauthorized")

    def make_read_only(self) -> None:
        self.memory.write_access = False

    def inject_failure(self, kind: str) -> None:
        self.memory.pending_failure = failure_for(kind)

    def disconnected(self) -> bool:
        return self.memory.disconnected

    def provider_calls(self) -> list[str]:
        return self.memory.calls
