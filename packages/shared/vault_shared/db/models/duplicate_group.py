import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class DuplicateGroup(Base):
    """One set of files sharing an identical content checksum, within one
    organization — the Storage Intelligence Layer's exact-duplicate unit.

    Deliberately keyed on `File.checksum` directly (Drive's own
    `md5Checksum`, populated at scan time — see `google_drive.py`), NOT on
    `FileMetadata.duplicate_group_key` (Phase 5's enrichment-owned field,
    which requires the enrichment pipeline to have run and caps a group at
    20 members for relationship-graph reasons unrelated to storage
    accounting). This lets duplicate detection run immediately after a
    scan, with no enrichment dependency, and report every member of a
    group regardless of size.

    One row per (organization, checksum) — upserted on every analysis run
    (identity key below), mirroring `Recommendation`'s
    upsert-by-rule-name-per-org convention rather than a delete-and-
    recreate pattern, so a group's id stays stable across runs for the
    `/v1/storage/duplicates/{id}` detail endpoint."""

    __tablename__ = "duplicate_groups"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "connector_id",
            "checksum",
            name="uq_duplicate_group_org_checksum",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Duplicates are found within one storage: a Drive file and a local file
    # are separate copies in separate places, never one group.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    storage_analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_analysis_jobs.id", ondelete="SET NULL"), nullable=True
    )

    checksum: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # `total_size_bytes` minus the largest member's size — the retained
    # file is never counted as recoverable (Phase 1 spec §6.4).
    recoverable_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    recommended_keep_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    recommended_keep_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recommended_keep_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
