"""The common storage contract.

AI Vault application code talks to storage only through `StorageAdapter`;
everything provider-specific (endpoints, auth headers, pagination tokens, MIME
quirks, error codes) lives behind an adapter. Adding a provider means writing
one adapter and registering it — no caller changes.

Rules every adapter follows:

* Addressed by *provider ids* (`ProviderFileId`), never by internal database
  ids or filesystem paths. See `vault_shared.storage.models`.
* Failures are raised as the normalized `vault_shared.storage.errors` types.
* An operation the provider lacks raises `StorageUnsupportedError`, and
  `capabilities()` says so up front. Adapters do not fake support.
* Reads are streams (`open_read`, `export`), so large files are never forced
  into memory; `read_bounded` is the safe way to get bytes when a caller
  really needs them.
* Mutating methods are only ever called by the Execution Engine — the single
  authority allowed to change user storage (Handbook §8.7). They are listed
  in `MUTATING_METHODS`, which the architecture tests enforce.
* Credentials never cross this boundary: an adapter is built with a
  `CredentialSource`, and no method accepts or returns a token.
* Nothing here is reachable from AI code: adapters are obtained from the
  registry by backend services only.
"""

from typing import Protocol, runtime_checkable

from vault_shared.storage.capabilities import StorageCapabilities
from vault_shared.storage.models import (
    ByteStream,
    ChangePage,
    ConnectionHealth,
    ExportFormat,
    ExportPurpose,
    FilePage,
    ProviderFileId,
    StorageContainer,
    StorageFile,
    StoragePermission,
    StorageQuota,
)

# Operations that change user storage. Only `ExecutionService` may call them.
MUTATING_METHODS = frozenset(
    {
        "rename",
        "move",
        "copy",
        "create_folder",
        "trash",
        "restore",
        "permanent_delete",
        "update_metadata",
        "upload",
        "empty_trash",
    }
)


@runtime_checkable
class StorageAdapter(Protocol):
    provider: str

    # -- connection ---------------------------------------------------------

    def capabilities(self) -> StorageCapabilities:
        """What this provider supports. Static and free — no network call."""
        ...

    def connect(self) -> None:
        """Make sure a usable credential exists for this connection, raising
        `StorageAuthenticationRequiredError` if the user must reconnect.
        Establishing a *new* connection (interactive consent) is not an
        adapter concern — an adapter represents a connection that exists."""
        ...

    def health(self) -> ConnectionHealth:
        """A cheap live probe. Expected failure states are returned as a
        status, not raised."""
        ...

    def disconnect(self) -> None:
        """Revoke the provider-side grant (best effort). Deleting AI Vault's
        own record of the connection is the caller's job."""
        ...

    def write_access_problems(self) -> list[str]:
        """Reasons this connection cannot perform mutations (for example a
        grant that was given read-only). Empty means it can. No network."""
        ...

    # -- discovery ----------------------------------------------------------

    def list_containers(self) -> list[StorageContainer]: ...

    def scan(self, container_id: str, cursor: str | None = None) -> FilePage:
        """One page of a full listing. Callers loop until `next_cursor` is
        None; that keeps progress, cancellation and checkpointing in the
        scanner."""
        ...

    def get_change_cursor(self, container_id: str) -> str:
        """The cursor to hand to `check_changes` after a full scan."""
        ...

    def check_changes(self, container_id: str, cursor: str) -> ChangePage: ...

    def get_file(self, file_id: ProviderFileId) -> StorageFile:
        """A live metadata read of one item, including its `Revision`."""
        ...

    def get_permissions(self, file_id: ProviderFileId) -> list[StoragePermission]: ...

    # -- reading ------------------------------------------------------------

    def open_read(self, file_id: ProviderFileId) -> ByteStream:
        """The item's raw bytes as a stream. Native documents have no raw
        bytes — use `export`."""
        ...

    def export(self, file_id: ProviderFileId, export_format: ExportFormat) -> ByteStream: ...

    def export_format_for(self, mime_type: str, purpose: ExportPurpose) -> ExportFormat | None:
        """How a provider-native document should be exported for `purpose`,
        or None when the MIME type is not a native document (or cannot be
        exported for that purpose)."""
        ...

    def is_native_document(self, mime_type: str) -> bool: ...

    def get_thumbnail(self, file_id: ProviderFileId) -> bytes | None: ...

    def storage_quota(self) -> StorageQuota: ...

    def list_trash(self) -> list[StorageFile]:
        """Files this account owns that sit in the provider's Trash."""
        ...

    # -- mutation (Execution Engine only) -----------------------------------

    def rename(self, file_id: ProviderFileId, new_name: str) -> StorageFile: ...

    def move(
        self,
        file_id: ProviderFileId,
        *,
        new_parent_id: ProviderFileId,
        old_parent_id: ProviderFileId,
    ) -> StorageFile: ...

    def copy(
        self,
        file_id: ProviderFileId,
        *,
        new_name: str | None = None,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile: ...

    def create_folder(
        self, name: str, *, parent_id: ProviderFileId | None = None
    ) -> StorageFile: ...

    def trash(self, file_id: ProviderFileId) -> StorageFile:
        """Idempotent: trashing a trashed item is not an error."""
        ...

    def restore(self, file_id: ProviderFileId) -> StorageFile:
        """Idempotent: restoring an item that is not trashed is not an error."""
        ...

    def permanent_delete(self, file_id: ProviderFileId) -> None: ...

    def empty_trash(self) -> None:
        """Permanently deletes everything `list_trash` returns. Unrecoverable."""
        ...

    def update_metadata(self, file_id: ProviderFileId, properties: dict[str, str]) -> StorageFile:
        """Provider-private key/value properties (for Drive, `appProperties`)."""
        ...

    def upload(
        self,
        name: str,
        content: ByteStream,
        *,
        mime_type: str,
        parent_id: ProviderFileId | None = None,
    ) -> StorageFile: ...
