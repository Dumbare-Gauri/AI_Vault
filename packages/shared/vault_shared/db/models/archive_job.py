import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class ArchiveJobStatus(enum.StrEnum):
    PENDING = "pending"
    CREATING = "creating"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class ArchiveJob(Base):
    """The Archive MVP's record of one compressed package created from a
    `CREATE_ARCHIVE` `ExecutionPlan` (Handbook §8.7 lineage — this table has
    no origin/existence outside that plan, `execution_plan_id` is unique).
    Unlike every existing execution action, `CREATE_ARCHIVE` never mutates
    Google Drive (files are only read); this row plus the zip object in
    `ObjectStorageClient`'s bucket are the operation's only real output.
    `manifest` is the authoritative record of what went in — one entry per
    archived file (id/name/path/size/mime/checksum) — captured once, at
    creation time, from files that may later be renamed/moved/trashed."""

    __tablename__ = "archive_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ArchiveJobStatus.PENDING
    )
    object_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compressed_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)

    # Where the archive lives in the user's own connected storage. A job is
    # only COMPLETED once the provider reports this file with the exact
    # size and checksum written (`verified_at`). The object-storage copy is
    # a secondary download cache, not the archive of record.
    destination_provider_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    destination_web_view_link: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    archive_md5: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Originals are only ever trashed after `verified_at` is set.
    remove_originals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    originals_removed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
