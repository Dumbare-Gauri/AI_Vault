import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class OrganizationRecommendationKind(enum.StrEnum):
    """The spec's full organization-action vocabulary. This phase's
    generator (`worker.organization.organization_recommendation_generator`)
    only ever emits the three `GROUP_*` kinds — one per scattered entity.
    The other five are reserved enum values for future, finer-grained,
    per-file proposals (a later single-file rename/move, reusing
    `RecommendationKind.RENAME`/`MOVE` the same way entity inference reuses
    `CLASSIFY`) — kept here now so that future addition is a new generator
    rule, not a migration."""

    CREATE_FOLDER = "create_folder"
    MOVE_FILE = "move_file"
    MOVE_FOLDER = "move_folder"
    RENAME_FILE = "rename_file"
    RENAME_FOLDER = "rename_folder"
    GROUP_PROJECT = "group_project"
    GROUP_CLIENT = "group_client"
    GROUP_CAMPAIGN = "group_campaign"


class OrganizationRecommendationStatus(enum.StrEnum):
    ACTIVE = "active"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"


class OrganizationRecommendation(Base):
    """One evidence-backed organization proposal (spec's Organization
    Recommendation Engine) — deliberately a separate table from
    `Recommendation`, whose identity (`UniqueConstraint(organization_id,
    rule_name)`, one row per deterministic rule per org) is the wrong shape
    for N independent per-entity proposals per org.

    `estimated_storage_impact_bytes` is the total size of files being
    *reorganized*, not reclaimed — reorganization moves bytes, it doesn't
    recover them. This must never be summed into `StorageAnalysisSnapshot.
    total_potential_savings_bytes`, which is specifically about duplicate/
    temporary-candidate reclaimable bytes — keeping these separate is what
    the spec's CURRENT/POTENTIAL/ACTUAL savings distinction protects.

    `execution_plan_id` is set once `apply()` dispatches the underlying
    `MOVE_FILE` ad-hoc plan (see `worker.organization.
    organization_recommendation_apply_service`) — that plan's own status is
    the ground truth for whether the move actually succeeded; this row's
    `status` only tracks whether the recommendation itself was applied."""

    __tablename__ = "organization_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_entities.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    reasoning_summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    affected_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    current_locations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    suggested_destination: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    estimated_storage_impact_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrganizationRecommendationStatus.ACTIVE
    )
    execution_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_plans.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
