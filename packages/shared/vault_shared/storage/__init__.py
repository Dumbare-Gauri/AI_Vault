"""The provider-independent storage layer (Handbook §8.1, ADR-025).

This package holds the *contract*: the adapter protocol, normalized models,
capability flags, normalized errors and the registry. It deliberately imports
no provider. Concrete adapters live in `vault_shared.storage.adapters` and are
wired in by `vault_shared.storage.default_registry`, which only composition
roots import.
"""

from vault_shared.storage.adapter import MUTATING_METHODS, StorageAdapter
from vault_shared.storage.capabilities import Capability, StorageCapabilities
from vault_shared.storage.credentials import CredentialSource
from vault_shared.storage.errors import (
    StorageAuthenticationRequiredError,
    StorageConflictError,
    StorageContentTooLargeError,
    StorageError,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StorageRateLimitedError,
    StorageStaleRevisionError,
    StorageUnauthorizedError,
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
from vault_shared.storage.reading import read_bounded
from vault_shared.storage.registry import (
    AdapterFactory,
    StaticAdapterProvider,
    StorageAdapterProvider,
    StorageAdapterRegistry,
)

__all__ = [
    "FOLDER_MIME_TYPE",
    "MUTATING_METHODS",
    "AdapterFactory",
    "ByteStream",
    "Capability",
    "ChangePage",
    "ConnectionHealth",
    "ContainerKind",
    "CredentialSource",
    "ExportFormat",
    "ExportPurpose",
    "FilePage",
    "HealthStatus",
    "ProviderFileId",
    "Revision",
    "StaticAdapterProvider",
    "StorageAdapter",
    "StorageAdapterProvider",
    "StorageAdapterRegistry",
    "StorageAuthenticationRequiredError",
    "StorageCapabilities",
    "StorageConflictError",
    "StorageContainer",
    "StorageContentTooLargeError",
    "StorageError",
    "StorageFile",
    "StorageForbiddenError",
    "StorageInvalidRequestError",
    "StorageNotFoundError",
    "StoragePermission",
    "StorageQuota",
    "StorageRateLimitedError",
    "StorageStaleRevisionError",
    "StorageUnauthorizedError",
    "StorageUnavailableError",
    "StorageUnsupportedError",
    "read_bounded",
]
