import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class DriveType(enum.StrEnum):
    MY_DRIVE = "my_drive"
    SHARED_DRIVE = "shared_drive"
    # An authorized folder or drive on a Local Agent's machine.
    LOCAL_FOLDER = "local_folder"


class StorageSource(Base):
    """One discoverable drive within a connected account — "My Drive" plus
    zero or more Shared Drives. A `StorageConnector` (Phase 3) is the OAuth
    connection to the account; a `StorageSource` is one of the drives that
    connection can see. `Folder`/`File` rows belong to a source, not
    directly to a connector, since one connector can expose many sources."""

    __tablename__ = "storage_sources"
    __table_args__ = (
        UniqueConstraint(
            "connector_id", "provider_drive_id", name="uq_storage_source_connector_drive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "root" for My Drive, or Google's shared-drive id.
    provider_drive_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    drive_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Google's startPageToken for this drive's Changes API — null until the
    # first full scan completes (Phase 3/4's "initial change-token strategy").
    change_token: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
