"""Storage that builds and unpacks archives itself.

The local agent zips files on the user's own disk, so the bytes never leave
the computer; Google Drive has no such operation and its archives are built
by the Execution Engine and uploaded. An adapter that can do it implements
this protocol; only `ExecutionService` calls it.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from vault_shared.storage.models import ProviderFileId, StorageFile

# Change user storage, like `MUTATING_METHODS`: the Execution Engine only.
NATIVE_ARCHIVE_METHODS = frozenset({"create_archive", "extract_archive"})


@dataclass(frozen=True)
class NativeArchive:
    """The archive written and checked by the storage, and what is in it.
    `entries`: `{file_id, arcname, size_bytes, compressed_bytes, stored}`;
    `skipped`: `{file_id, reason}`."""

    file: StorageFile
    entries: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class NativeExtraction:
    """The new folder an archive was unpacked into, everything created in
    it, and the entries left out (`{name, reason}`)."""

    folder: StorageFile
    created: list[StorageFile] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


@runtime_checkable
class NativeArchiver(Protocol):
    def create_archive(
        self,
        items: list[tuple[ProviderFileId, str]],
        *,
        parent_id: ProviderFileId,
        name: str,
        expected_bytes: int = 0,
    ) -> NativeArchive: ...

    def extract_archive(
        self, file_id: ProviderFileId, *, parent_id: ProviderFileId, expected_bytes: int = 0
    ) -> NativeExtraction: ...

    def archive_contents(self, file_id: ProviderFileId, *, expected_bytes: int = 0) -> dict:
        """Read only: `{entries, entry_count, file_count, total_bytes,
        unsafe_count}` from the archive's own index."""
        ...

    def get_file(self, file_id: ProviderFileId) -> StorageFile: ...

    def trash(self, file_id: ProviderFileId) -> StorageFile: ...
