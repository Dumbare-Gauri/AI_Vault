import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class MemoryType(enum.StrEnum):
    NAMING_CONVENTION = "naming_convention"
    PREFERRED_STRUCTURE = "preferred_structure"
    CORRECTION = "correction"


class OrganizationMemory(Base):
    """A structured fact the organization has taught this system — not raw
    model fine-tuning, a plain row a future `AIContext` can read back (spec:
    "organizational memory... structured user corrections"). Written from
    exactly one place today: `ExecutionPlanService.create_ad_hoc_plan`'s
    `MOVE_FILE` branch, when a user moves a file somewhere other than what
    an active `OrganizationRecommendation` suggested for it — a
    `CORRECTION` row with `confidence=1.0` (an observed fact, not an
    inference). Fed back into `AIContext.naming_conventions`/`constraints`
    (existing fields, no schema change) when building context for a future
    entity-inference cluster touching the same entity."""

    __tablename__ = "organization_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    memory_type: Mapped[str] = mapped_column(String(30), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
