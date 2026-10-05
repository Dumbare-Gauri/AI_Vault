import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class ExecutionPlanStatus(enum.StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIALLY_COMPLETED = "partially_completed"
    ROLLED_BACK = "rolled_back"


class ExecutionPlan(Base):
    """A deterministic, reviewable translation of one `Recommendation` OR
    one Storage Intelligence `DuplicateGroup` into ordered `ExecutionStep`s
    (Handbook §8.7's Execution Engine, Phase 8 spec's Execution Planner;
    the `DuplicateGroup` origin added post-hardening — ADR-024). Exactly
    one of `recommendation_id`/`duplicate_group_id` is ever set, enforced
    by a DB check constraint, not just application logic. Creating a plan
    performs no mutating Drive call itself — it only reads already-stored
    state — and always creates a companion `ApprovalRequest` in the same
    transaction, since the Core Philosophy's lifecycle
    (origin → Plan → Human Review → Approval → Execution) has no state
    where a plan exists without something pending review."""

    __tablename__ = "execution_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    duplicate_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("duplicate_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=ExecutionPlanStatus.PENDING_APPROVAL, index=True
    )
    target_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    estimated_impact: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_storage_savings_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    rollback_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    required_permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
