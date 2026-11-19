import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class EntityType(enum.StrEnum):
    PROJECT = "project"
    CLIENT = "client"
    CAMPAIGN = "campaign"


class EntityStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class EntityLinkSource(enum.StrEnum):
    AI_INFERRED = "ai_inferred"
    USER_ASSIGNED = "user_assigned"


class OrganizationEntity(Base):
    """A Project/Client/Campaign discovered (or confirmed) in one
    organization's Drive — the Intelligence Engine's identity registry for
    entity inference (spec: "Project/Client/Campaign detection with
    configurable evidence thresholds"). One polymorphic table with an
    `entity_type` discriminator, matching this codebase's own convention
    (`Recommendation`'s `category`, `KnowledgeAttribute`'s `attribute_type`)
    rather than three near-identical tables.

    `normalized_name` (lowercased, whitespace-collapsed) is the dedup key —
    `(organization_id, entity_type, normalized_name)` is unique, so the same
    label value from different files/clusters/analysis runs always resolves
    to the same row rather than accumulating near-duplicate entities."""

    __tablename__ = "organization_entities"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_type",
            "normalized_name",
            name="uq_organization_entity_org_type_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EntityStatus.ACTIVE)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FileEntityLink(Base):
    """One file's link to an `OrganizationEntity` — "Unknown is always
    allowed" is realized by this row's *absence*, never a fabricated
    `UNKNOWN` entity. `evidence` here is link-level (why *this file* maps to
    the entity), distinct from the entity's own `evidence` (why the entity
    itself exists). `inference_version` lets a re-run of entity inference
    skip files already linked at the current scorer version without
    reprocessing everything."""

    __tablename__ = "file_entity_links"
    __table_args__ = (UniqueConstraint("file_id", "entity_id", name="uq_file_entity_link"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )

    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    inference_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
