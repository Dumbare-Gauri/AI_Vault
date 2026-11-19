import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class InsightRecord(Base):
    """A read-only, informational observation for the dashboard's "AI
    Insights" section (Phase 7 spec) — deliberately distinct from
    `Recommendation`: an insight has no risk level, no suggested action, no
    approval workflow, and is never resolved/superseded — it's a snapshot
    observation ("these documents look foundational," "this file is a
    size outlier"), not an open issue to fix. Append-only by design (one
    row per `RecommendationJob` run that found something notable), so the
    dashboard's "recently generated insights" feed is just the newest N
    rows — this is what gives the dashboard a naturally accumulating
    history without any upsert/resolve logic."""

    __tablename__ = "insight_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The storage this insight was computed from.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recommendation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recommendation_jobs.id", ondelete="SET NULL"), nullable=True
    )
    insight_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    related_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
