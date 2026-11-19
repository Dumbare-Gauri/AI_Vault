import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class DashboardSnapshot(Base):
    """A point-in-time aggregate of one organization's storage/knowledge/AI
    metrics (Phase 7 spec's "Dashboard Snapshot" — "keep historical data so
    trends can be visualized over time"). Append-only, one row per
    `RecommendationJob` run — the dashboard's trend charts (storage growth,
    knowledge-completeness over time) are just this table's rows for an
    organization, ordered by `created_at`; no separate time-series store."""

    __tablename__ = "dashboard_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The storage these totals describe — totals are never combined across
    # storages.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recommendation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recommendation_jobs.id", ondelete="SET NULL"), nullable=True
    )

    connected_providers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_folders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    classified_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unclassified_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_enrichment_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relationship_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_recommendations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # classified_files / total_files, 0 when there are no files yet — the
    # single headline "Knowledge Health" number the spec's "Knowledge
    # completeness score" asks for.
    knowledge_completeness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
