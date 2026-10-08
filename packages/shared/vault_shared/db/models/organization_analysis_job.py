import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class OrganizationAnalysisJobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OrganizationAnalysisJob(Base):
    """One run of the "Analyze Organization" pass (spec section 16's
    "Organize my Drive" workflow) — entity clustering+inference, lifecycle
    scoring, and recommendation generation, in that order (`worker.
    organization.organization_analysis_service`). Organization-scoped, not
    connector-scoped (unlike `IntelligenceJob`/`EmbeddingJob`) — this is a
    cross-connector pass over an org's already-enriched files, and it is
    user-triggered only (`POST /v1/organization/analyze`), never
    auto-chained off enrichment/embedding completion, since entity
    inference has real per-run GLM cost."""

    __tablename__ = "organization_analysis_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrganizationAnalysisJobStatus.PENDING
    )
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clusters_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entities_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendations_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
