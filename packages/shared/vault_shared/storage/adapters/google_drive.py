"""`GoogleDriveAdapter` — Google Drive as one implementation of the common
storage contract.

It wraps the existing `GoogleDriveClient` (a leaf that only knows Drive's HTTP
API) rather than replacing it, and adds what the contract needs on top:

* credentials come from a `CredentialSource`, never as an argument, so the
  client is always handed a fresh valid token and no caller sees one;
* Drive's shapes (`DriveFile`, page tokens, `drive_id=None` for "My Drive",
  the Google-native MIME types and their export tables) are translated into
  the provider-neutral models here, and nowhere else;
* every failure leaves as a normalized `StorageError`;
* each method makes exactly one provider request (the token is cached), so
  the abstraction adds no N+1 traffic.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from vault_shared.connectors.google_drive import (
    PROVIDER_NAME,
    DriveFile,
    DrivePermission,
    GoogleDriveClient,
)
from vault_shared.connectors.google_workspace import DRIVE_WRITE_SCOPE
from vault_shared.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    NotFoundError,
    ReauthRequiredError,
    UnauthorizedError,
    ValidationError,
)
from vault_shared.storage.capabilities import StorageCapabilities
from vault_shared.storage.credentials import CredentialSource
from vault_shared.storage.errors import (
    StorageAuthenticationRequiredError,
    StorageConflictError,
    StorageError,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StorageUnauthorizedError,
    StorageUnavailableError,
)
from vault_shared.storage.models import (
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

_PERSONAL_CONTAINER_ID = "root"
_NATIVE_MIME_PREFIX = "application/vnd.google-apps."

_DOC = "application/vnd.google-apps.document"
_SLIDES = "application/vnd.google-apps.presentation"
_SHEET = "application/vnd.google-apps.spreadsheet"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_PDF_OR_XLSX = {
    _DOC: ExportFormat("application/pdf", "pdf", "google_doc_pdf"),
    _SLIDES: ExportFormat("application/pdf", "pdf", "google_slides_pdf"),
    _SHEET: ExportFormat(_XLSX, "xlsx", "google_sheets_xlsx"),
}

# Which format a native document is exported to depends on why it is being
# read: text for content extraction (search indexing), a viewable file for a
# user download, a faithful backup format for archives.
_EXPORTS: dict[ExportPurpose, dict[str, ExportFormat]] = {
    ExportPurpose.TEXT_EXTRACTION: {
        _DOC: ExportFormat("text/plain", "txt", "google_doc_export"),
        _SLIDES: ExportFormat("text/plain", "txt", "google_slides_export"),
        _SHEET: ExportFormat("text/csv", "csv", "google_sheets_export"),
    },
    ExportPurpose.DOWNLOAD: _PDF_OR_XLSX,
    ExportPurpose.ARCHIVE: _PDF_OR_XLSX,
}

_ROLE_MAP = {
    "owner": "owner",
    "organizer": "writer",
    "fileOrganizer": "writer",
    "writer": "writer",
    "commenter": "commenter",
    "reader": "reader",
}

GOOGLE_DRIVE_CAPABILITIES = StorageCapabilities(
    supports_trash=True,
    supports_restore=True,
    supports_permanent_delete=True,
    supports_copy=True,
    supports_create_folder=True,
    supports_upload=True,
    supports_download=True,
    supports_change_feed=True,
    supports_thumbnails=True,
    supports_permissions=True,
    supports_native_export=True,
    supports_streaming=True,
    supports_hash=True,
    supports_metadata_update=True,
)


def _normalize(exc: Exception) -> StorageError | None:
    """Maps a pre-existing application error onto the storage model. Returns
    None for anything that is not a storage-relevant failure, which then
    propagates unchanged."""
    if isinstance(exc, StorageError):
        return exc
    if isinstance(exc, ReauthRequiredError):
        return StorageAuthenticationRequiredError(exc.message, provider_code="reauth_required")
    if isinstance(exc, UnauthorizedError):
        return StorageUnauthorizedError(exc.message, provider_code="unauthorized")
    if isinstance(exc, ForbiddenError):
        return StorageForbiddenError(exc.message, provider_code="forbidden")
    if isinstance(exc, NotFoundError):
        return StorageNotFoundError(exc.message, provider_code="not_found")
    if isinstance(exc, ConflictError):
        return StorageConflictError(exc.message, provider_code="conflict")
    if isinstance(exc, ValidationError):
        return StorageInvalidRequestError(exc.message, provider_code="invalid_request")
    if isinstance(exc, DependencyUnavailableError):
        return StorageUnavailableError(exc.message, provider_code="unavailable")
    return None


class GoogleDriveAdapter:
    provider = PROVIDER_NAME

    def __init__(
        self,
        *,
        client: GoogleDriveClient,
        credentials: CredentialSource,
        connection_id: uuid.UUID | None = None,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._connection_id = connection_id

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _translated(self) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            normalized = _normalize(exc)
            if normalized is None:
                raise
            normalized.with_provider(self.provider)
            if normalized is exc:
                raise
            raise normalized from exc

    def _token(self) -> str:
        try:
            return self._credentials.access_token()
        except NotFoundError as exc:
            raise StorageAuthenticationRequiredError(
                "The connection has no stored credentials.",
                provider=self.provider,
                provider_code="no_credentials",
            ) from exc

    def _to_storage_file(self, item: DriveFile) -> StorageFile:
        parent_ids = tuple(ProviderFileId(parent) for parent in item.parents)
        return StorageFile(
            provider=self.provider,
            provider_file_id=ProviderFileId(item.id),
            name=item.name,
            mime_type=item.mime_type,
            is_folder=item.is_folder,
            trashed=item.trashed,
            connection_id=self._connection_id,
            size_bytes=item.size,
            parent_id=parent_ids[0] if parent_ids else None,
            parent_ids=parent_ids,
            created_at=item.created_time,
            modified_at=item.modified_time,
            accessed_at=item.viewed_by_me_time,
            owner_email=item.owner_email,
            shared=item.shared,
            checksum=item.checksum,
            revision=Revision(token=item.version_id, modified_at=item.modified_time),
            web_view_link=item.web_view_link,
            native_document=self.is_native_document(item.mime_type),
        )

    @staticmethod
    def _drive_id(container_id: str) -> str | None:
        return None if container_id == _PERSONAL_CONTAINER_ID else container_id

    # -- connection ---------------------------------------------------------

    def capabilities(self) -> StorageCapabilities:
        return GOOGLE_DRIVE_CAPABILITIES

    def connect(self) -> None:
        with self._translated():
            self._token()

    def health(self) -> ConnectionHealth:
        try:
            with self._translated():
                email = self._client.get_account_email(access_token=self._token())
        except (StorageAuthenticationRequiredError, StorageUnauthorizedError):
            return ConnectionHealth(
                status=HealthStatus.AUTHENTICATION_REQUIRED,
                detail="Google Drive access must be reconnected.",
            )
        except StorageError as exc:
            return ConnectionHealth(status=HealthStatus.DEGRADED, detail=exc.message)
        return ConnectionHealth(status=HealthStatus.OK, account=email)

    def disconnect(self) -> None:
        with self._translated():
            self._credentials.revoke()

    def write_access_problems(self) -> list[str]:
        scopes = self._credentials.granted_scopes()
        if scopes is None:
            return ["Connector has no stored credentials."]
        if DRIVE_WRITE_SCOPE not in scopes:
            return [
                "Connector was authorized without Drive write access — reconnect Google "
                "Workspace to grant it before this plan can execute."
            ]
        return []

    # -- discovery ----------------------------------------------------------

    def list_containers(self) -> list[StorageContainer]:
        with self._translated():
            shared_drives = self._client.list_shared_drives(access_token=self._token())
        return [
            StorageContainer(_PERSONAL_CONTAINER_ID, "My Drive", ContainerKind.PERSONAL),
            *(StorageContainer(d.id, d.name, ContainerKind.SHARED) for d in shared_drives),
        ]

    def scan(self, container_id: str, cursor: str | None = None) -> FilePage:
        with self._translated():
            page = self._client.list_files_page(
                access_token=self._token(),
                drive_id=self._drive_id(container_id),
                page_token=cursor,
            )
        return FilePage(
            files=tuple(self._to_storage_file(item) for item in page.files),
            next_cursor=page.next_page_token,
        )

    def get_change_cursor(self, container_id: str) -> str:
        with self._translated():
            return self._client.get_start_page_token(
                access_token=self._token(), drive_id=self._drive_id(container_id)
            )

    def check_changes(self, container_id: str, cursor: str) -> ChangePage:
        with self._translated():
            page = self._client.list_changes_page(
                access_token=self._token(),
                page_token=cursor,
                drive_id=self._drive_id(container_id),
            )
        return ChangePage(
            changed=tuple(self._to_storage_file(item) for item in page.changed_files),
            removed_ids=tuple(ProviderFileId(file_id) for file_id in page.removed_file_ids),
            next_cursor=page.next_page_token,
            new_start_cursor=page.new_start_page_token,
        )

    def get_file(self, file_id: ProviderFileId) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.get_file(access_token=self._token(), file_id=file_id)
            )

    def get_permissions(self, file_id: ProviderFileId) -> list[StoragePermission]:
        with self._translated():
            permissions = self._client.list_permissions(access_token=self._token(), file_id=file_id)
        return [_to_permission(item) for item in permissions]

    # -- reading ------------------------------------------------------------

    def open_read(self, file_id: ProviderFileId) -> ByteStream:
        with self._translated():
            return self._client.stream_file(access_token=self._token(), file_id=file_id)

    def export(self, file_id: ProviderFileId, export_format: ExportFormat) -> ByteStream:
        with self._translated():
            return self._client.stream_export(
                access_token=self._token(),
                file_id=file_id,
                export_mime_type=export_format.mime_type,
            )

    def export_format_for(self, mime_type: str, purpose: ExportPurpose) -> ExportFormat | None:
        return _EXPORTS[purpose].get(mime_type)

    def is_native_document(self, mime_type: str) -> bool:
        return mime_type.startswith(_NATIVE_MIME_PREFIX)

    def get_thumbnail(self, file_id: ProviderFileId) -> bytes | None:
        with self._translated():
            return self._client.get_thumbnail(access_token=self._token(), file_id=file_id)

    def list_trash(self) -> list[StorageFile]:
        with self._translated():
            return [
                self._to_storage_file(item)
                for item in self._client.list_trashed_files(access_token=self._token())
            ]

    def storage_quota(self) -> StorageQuota:
        with self._translated():
            used, total, trash = self._client.get_storage_quota(access_token=self._token())
        return StorageQuota(used_bytes=used, total_bytes=total, trash_bytes=trash)

    # -- mutation (Execution Engine only) -----------------------------------

    def rename(self, file_id: ProviderFileId, new_name: str) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.rename_file(
                    access_token=self._token(), file_id=file_id, new_name=new_name
                )
            )

    def move(
        self,
        file_id: ProviderFileId,
        *,
        new_parent_id: ProviderFileId,
        old_parent_id: ProviderFileId,
    ) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.move_file(
                    access_token=self._token(),
                    file_id=file_id,
                    add_parent_id=new_parent_id,
                    remove_parent_id=old_parent_id,
                )
            )

    def copy(
        self,
        file_id: ProviderFileId,
        *,
        new_name: str | None = None,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.copy_file(
                    access_token=self._token(),
                    file_id=file_id,
                    new_name=new_name,
                    parent_id=parent_id,
                )
            )

    def create_folder(self, name: str, *, parent_id: ProviderFileId | None = None) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.create_folder(
                    access_token=self._token(), name=name, parent_id=parent_id
                )
            )

    def trash(self, file_id: ProviderFileId) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.set_trashed(access_token=self._token(), file_id=file_id, trashed=True)
            )

    def restore(self, file_id: ProviderFileId) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.set_trashed(access_token=self._token(), file_id=file_id, trashed=False)
            )

    def permanent_delete(self, file_id: ProviderFileId) -> None:
        with self._translated():
            self._client.delete_file(access_token=self._token(), file_id=file_id)

    def empty_trash(self) -> None:
        with self._translated():
            self._client.empty_drive_trash(access_token=self._token())

    def update_metadata(self, file_id: ProviderFileId, properties: dict[str, str]) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.update_app_properties(
                    access_token=self._token(), file_id=file_id, properties=properties
                )
            )

    def upload(
        self,
        name: str,
        content: ByteStream,
        *,
        mime_type: str,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile:
        with self._translated():
            return self._to_storage_file(
                self._client.upload_file(
                    access_token=self._token(),
                    name=name,
                    parent_id=parent_id,
                    mime_type=mime_type,
                    chunks=content,
                )
            )


def _to_permission(item: DrivePermission) -> StoragePermission:
    principal = item.email_address or item.domain
    return StoragePermission(
        kind=item.type, role=_ROLE_MAP.get(item.role, item.role), principal=principal
    )
