"""Provider-neutral storage representations.

Identity — three different things that must never be confused:

* the AI Vault *file id* (`File.id`, a UUID) — internal, meaningless to any
  provider, never passed to an adapter;
* the *provider file id* (`File.provider_file_id`, a string) — what an adapter
  is addressed with (`ProviderFileId`);
* the *storage connection id* (`StorageConnector.id`) — which linked account
  an adapter instance is bound to. An adapter is created for one connection
  and cannot address another's items.

`StorageFile` is a provider-facing snapshot, not the database `File`: the
application layer maps it into the existing models.

Paths: providers differ (Google Drive is id/parent based, Dropbox and local
disks are path based), so every mutation is addressed by provider ids. A
`path` is informational only and business logic must not derive behavior from
it.
"""

import enum
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NewType

# The provider's own id for an item. A distinct type (not a bare `str`) so a
# database UUID or a stringified internal id cannot be passed by accident
# without an explicit, greppable `ProviderFileId(...)` at the call site.
ProviderFileId = NewType("ProviderFileId", str)

# The MIME type folders are stored under. Existing inventory rows hold the
# value the first provider (Google Drive) reports, so every adapter maps its
# folders to this until a data migration normalizes stored rows.
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

ByteStream = Iterator[bytes]


@dataclass(frozen=True)
class Revision:
    """A normalized version marker for stale-state detection.

    `token` is the provider's content-revision identifier when it has one
    (Drive `headRevisionId`, Dropbox `rev`, an ETag, a content hash);
    `modified_at` is the provider's modified time. Providers differ in which
    they can supply, so comparison is deliberately conservative: two
    revisions are only declared different when both carry a token and the
    tokens disagree. A timestamp alone is too noisy — metadata-only changes
    (a rename, a share) move it — and a missing token means the provider
    offers no content revision for the item, in which case nothing can be
    concluded.
    """

    token: str | None = None
    modified_at: datetime | None = None

    def differs_from(self, other: "Revision") -> bool:
        return self.token is not None and other.token is not None and self.token != other.token

    def to_dict(self) -> dict[str, str | None]:
        return {
            "token": self.token,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> "Revision | None":
        if not isinstance(data, dict):
            return None
        token = data.get("token")
        modified_raw = data.get("modified_at")
        modified_at: datetime | None = None
        if isinstance(modified_raw, str):
            try:
                modified_at = datetime.fromisoformat(modified_raw)
            except ValueError:
                modified_at = None
        return cls(token=token if isinstance(token, str) else None, modified_at=modified_at)


@dataclass(frozen=True)
class StorageFile:
    """One item (file or folder) as an adapter reports it."""

    provider: str
    provider_file_id: ProviderFileId
    name: str
    mime_type: str
    is_folder: bool
    trashed: bool = False
    connection_id: uuid.UUID | None = None
    size_bytes: int | None = None
    parent_id: ProviderFileId | None = None
    parent_ids: tuple[ProviderFileId, ...] = ()
    path: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    accessed_at: datetime | None = None
    owner_email: str | None = None
    shared: bool = False
    checksum: str | None = None
    revision: Revision = field(default_factory=Revision)
    web_view_link: str | None = None
    # A provider-native document with no raw bytes of its own (a Google Doc):
    # it can only be read by exporting it to another format.
    native_document: bool = False

    @property
    def extension(self) -> str | None:
        _stem, dot, suffix = self.name.rpartition(".")
        return suffix.lower() if dot and suffix and _stem else None


class ContainerKind(enum.StrEnum):
    PERSONAL = "personal"
    SHARED = "shared"


@dataclass(frozen=True)
class StorageContainer:
    """A top-level space an adapter can scan on its own: a user's own drive
    or a shared/team drive. `id` is what `scan()` and `check_changes()`
    take."""

    id: str
    name: str
    kind: ContainerKind


@dataclass(frozen=True)
class FilePage:
    files: tuple[StorageFile, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ChangePage:
    changed: tuple[StorageFile, ...]
    removed_ids: tuple[ProviderFileId, ...]
    next_cursor: str | None
    new_start_cursor: str | None


class HealthStatus(enum.StrEnum):
    OK = "ok"
    AUTHENTICATION_REQUIRED = "authentication_required"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ConnectionHealth:
    status: HealthStatus
    account: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.OK


@dataclass(frozen=True)
class StorageQuota:
    """Provider-reported account storage. `total_bytes` is None when the
    provider reports no limit (e.g. an unlimited Workspace plan)."""

    used_bytes: int | None
    total_bytes: int | None
    # Part of `used_bytes` taken by files in the provider's Trash.
    trash_bytes: int | None = None


@dataclass(frozen=True)
class StoragePermission:
    """A normalized sharing entry: who has what kind of access."""

    kind: str  # "user" | "group" | "domain" | "anyone"
    role: str  # "owner" | "writer" | "commenter" | "reader" (provider roles mapped)
    principal: str | None = None


class ExportPurpose(enum.StrEnum):
    """Why a native document is being read. The right target format differs:
    text for content extraction, a viewable file for a user download, a
    faithful backup format for archives."""

    DOWNLOAD = "download"
    ARCHIVE = "archive"
    TEXT_EXTRACTION = "text_extraction"


@dataclass(frozen=True)
class ExportFormat:
    mime_type: str
    extension: str
    # A short provider-defined name recorded as the "extractor" in stored
    # extraction results (e.g. "google_doc_export").
    label: str


def utc_now() -> datetime:
    return datetime.now(UTC)
