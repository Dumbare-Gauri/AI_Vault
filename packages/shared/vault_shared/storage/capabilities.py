"""What a storage provider can and cannot do.

Business logic asks the adapter (`adapter.capabilities().supports_trash`)
instead of comparing provider names, and a provider that lacks an operation
says so explicitly rather than pretending: calling an unsupported operation
raises `StorageUnsupportedError`.
"""

import enum
from dataclasses import dataclass

from vault_shared.storage.errors import StorageUnsupportedError


class Capability(enum.StrEnum):
    TRASH = "supports_trash"
    RESTORE = "supports_restore"
    PERMANENT_DELETE = "supports_permanent_delete"
    COPY = "supports_copy"
    CREATE_FOLDER = "supports_create_folder"
    UPLOAD = "supports_upload"
    DOWNLOAD = "supports_download"
    CHANGE_FEED = "supports_change_feed"
    THUMBNAILS = "supports_thumbnails"
    PERMISSIONS = "supports_permissions"
    NATIVE_EXPORT = "supports_native_export"
    STREAMING = "supports_streaming"
    HASH = "supports_hash"
    METADATA_UPDATE = "supports_metadata_update"


@dataclass(frozen=True)
class StorageCapabilities:
    """Everything defaults to unsupported, so a new adapter only declares
    what it really implements."""

    supports_trash: bool = False
    supports_restore: bool = False
    supports_permanent_delete: bool = False
    supports_copy: bool = False
    supports_create_folder: bool = False
    supports_upload: bool = False
    supports_download: bool = False
    supports_change_feed: bool = False
    supports_thumbnails: bool = False
    supports_permissions: bool = False
    supports_native_export: bool = False
    supports_streaming: bool = False
    supports_hash: bool = False
    supports_metadata_update: bool = False

    def supports(self, capability: Capability) -> bool:
        return bool(getattr(self, capability.value))

    def supported(self) -> frozenset[Capability]:
        return frozenset(cap for cap in Capability if self.supports(cap))

    def require(self, capability: Capability, *, provider: str) -> None:
        if not self.supports(capability):
            operation = capability.value.removeprefix("supports_")
            raise StorageUnsupportedError(
                f"This storage provider does not support {operation}.", provider=provider
            )

