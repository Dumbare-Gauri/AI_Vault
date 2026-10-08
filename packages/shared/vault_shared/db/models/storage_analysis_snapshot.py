import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class StorageAnalysisSnapshot(Base):
    """A point-in-time aggregate of one organization's storage breakdown and
    potential savings — mirrors `DashboardSnapshot` exactly (append-only,
    one row per `StorageAnalysisJob` run, trend = this table's rows ordered
    by `created_at`, no separate time-series store), kept as its own table
    rather than new columns on `DashboardSnapshot` to keep the two engines'
    concerns (knowledge/AI health vs. storage/cost health) independently
    evolvable."""

    __tablename__ = "storage_analysis_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_analysis_jobs.id", ondelete="SET NULL"), nullable=True
    )
    # NULL for the whole organization; set for one connected storage's own view.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )

    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_folders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # {"images": 123456, "videos": ..., ...} — bytes per deterministic MIME
    # category (see `storage_intelligence/mime_category.py`).
    breakdown_by_type_bytes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {"< 1 MB": ..., "1-10 MB": ..., ...} — bytes per fixed size bucket.
    breakdown_by_size_bucket_bytes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # {"<storage_source_id>": {"name": ..., "bytes": ...}, ...}
    breakdown_by_source_bytes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    duplicate_group_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_recoverable_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    large_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    large_file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    old_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    old_file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    inactive_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inactive_file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    temporary_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    temporary_candidate_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Deduplicated union of (duplicate-recoverable ∪ temporary-candidate)
    # file ids, summed once each — never the naive sum of the two
    # categories above (Phase 1 spec §12: "avoid double counting").
    total_potential_savings_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
