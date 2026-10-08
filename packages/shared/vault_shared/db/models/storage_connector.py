import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vault_shared.db.session import Base

if TYPE_CHECKING:
    from vault_shared.db.models.connector_credentials import ConnectorCredentials


class ConnectorProvider(enum.StrEnum):
    """Only Google Workspace exists today — the column is a string, not a DB
    enum, so adding OneDrive/Dropbox/S3/NAS (Handbook §18) is a data value,
    never a migration."""

    GOOGLE_WORKSPACE = "google_workspace"
    # A folder or drive on the user's own machine, through the Local Agent.
    LOCAL_AGENT = "local_agent"


# Providers whose write access is an OAuth scope granted at connect time.
OAUTH_SCOPED_PROVIDERS = frozenset({ConnectorProvider.GOOGLE_WORKSPACE})

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    ConnectorProvider.GOOGLE_WORKSPACE: "Google Drive",
    ConnectorProvider.LOCAL_AGENT: "this computer",
}


_PROVIDER_LOCATIONS: dict[str, str] = {
    ConnectorProvider.GOOGLE_WORKSPACE: "in Google Drive",
    ConnectorProvider.LOCAL_AGENT: "on this computer",
}


def provider_location(provider: str) -> str:
    """Where a provider's files are, as words: "in Google Drive", "on this
    computer"."""
    return _PROVIDER_LOCATIONS.get(provider, "in your storage")


def provider_display_name(provider: str) -> str:
    """What a user calls the storage a connection points at — the one place
    a provider id becomes words, so business code never names a provider."""
    return _PROVIDER_DISPLAY_NAMES.get(provider, "your storage")


class ConnectorStatus(enum.StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"
    # Distinct from ERROR: the provider has definitively rejected the stored
    # refresh token (Google's `invalid_grant`) — no retry or background
    # refresh can ever recover this, only the user reconnecting can. `status`
    # is a plain String(20) column (see class docstring), so this is a data
    # value, not a migration.
    REAUTH_REQUIRED = "reauth_required"


class StorageConnector(Base):
    """One row per (organization, provider) — reconnecting after a disconnect
    updates this same row rather than creating a new one, which is also what
    makes "reject a duplicate connect attempt while already connected"
    (Phase 3 spec's error-handling requirement) a simple status check.

    No `synchronization_metadata` table yet (the phase spec lists it as
    "placeholder only" since this phase must not scan storage) — a single
    nullable `last_synced_at` column here is enough of a placeholder for
    Phase 4 to build on; a dedicated table is deferred until there's a real
    sync-cursor shape to store.
    """

    __tablename__ = "storage_connectors"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", name="uq_connector_org_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ConnectorStatus.PENDING)

    connected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    workspace_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Provider-reported account storage, refreshed on verify and after each
    # scan. `storage_total_bytes` stays None for an unlimited plan.
    storage_used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_trash_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quota_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    credentials: Mapped["ConnectorCredentials | None"] = relationship(
        back_populates="connector", uselist=False, cascade="all, delete-orphan"
    )
