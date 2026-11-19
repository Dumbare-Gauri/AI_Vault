import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class RecommendationCategory(enum.StrEnum):
    STORAGE_OPTIMIZATION = "storage_optimization"
    KNOWLEDGE_OPTIMIZATION = "knowledge_optimization"
    SECURITY = "security"
    COLLABORATION = "collaboration"
    PRODUCTIVITY = "productivity"


class RecommendationRiskLevel(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendationStatus(enum.StrEnum):
    """`ACTIVE`: the condition that produced this recommendation still
    holds as of the most recent `RecommendationJob` run. `RESOLVED`: the
    condition no longer holds (e.g. the duplicate files were cleaned up
    outside this platform, or a rescan simply changed the data) — set
    automatically by `RecommendationRepository.resolve_stale`, never
    deleted, so historical recommendations remain visible for trend
    analysis (Phase 7 spec's "keep historical data so trends can be
    visualized over time")."""

    ACTIVE = "active"
    RESOLVED = "resolved"


class Recommendation(Base):
    """One explainable, non-executing suggestion (Handbook's Recommendation
    Engine) — organization-scoped. Identity for recompute purposes is
    `(organization_id, rule_name)`: each deterministic rule produces at
    most one aggregate recommendation per organization per run (e.g. "14
    duplicate groups found, ~2.1 GB reclaimable" rather than one row per
    duplicate group) — this is what keeps `RecommendationRepository.upsert`
    simple and the Recommendation Center's list bounded and readable,
    consistent with ADR-017's "bounded and explainable over spec-literal
    completeness" precedent. `affected_file_ids`/`related_departments` are
    plain JSONB arrays (not join tables) for the same reason Phase 5 used a
    JSONB `metadata` column on `FileRelationship` — a recommendation
    explaining itself to a founder needs to list files, not support
    relational queries *from* a file back to its recommendations."""

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "connector_id", "rule_name", name="uq_recommendation_org_rule"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The storage this recommendation is about — rules run per storage, so a
    # recommendation never mixes Google Drive and local files.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recommendation_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The exact rule that produced this row (e.g. "duplicate_files") — both
    # the recompute identity above and the traceability the phase spec
    # explicitly requires ("Traceable to its source data").
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_impact: Mapped[str] = mapped_column(String(500), nullable=False)
    # A numeric magnitude backing `estimated_impact`'s prose (bytes, file
    # count, etc.) — nullable since not every recommendation has one
    # comparable number; used only for priority scoring, never rendered
    # directly (the prose in `estimated_impact` is what a founder reads).
    impact_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    # Always true in this phase — nothing this platform does executes a
    # recommendation yet (Phase 7 spec: "No recommendation is executed
    # automatically"); the column exists now so Phase 8's Approval &
    # Execution System has something to gate on without a migration.
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    related_departments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RecommendationStatus.ACTIVE, index=True
    )
    # Computed by `worker.recommendation.priority.score_priority` — an
    # explainable, weighted combination of confidence/risk/impact (Phase 7
    # spec: "ranking algorithm should be explainable and configurable").
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
