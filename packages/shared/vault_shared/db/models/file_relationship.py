import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class RelationshipType(enum.StrEnum):
    SEQUENTIAL_VERSION = "sequential_version"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    SHARED_OWNERSHIP = "shared_ownership"
    # Phase 2 — embedding-based similarity (`worker.relationships.
    # near_duplicate_service`), computed off `EmbeddingJob` completion
    # since it needs `Embedding` rows that don't exist yet when
    # `RelationshipDiscoveryService.discover()` runs. Excludes pairs
    # already sharing a DUPLICATE_CANDIDATE edge (same checksum).
    NEAR_DUPLICATE = "near_duplicate"


class FileRelationship(Base):
    """A discovered, explainable edge between two files (Handbook §8.4's
    Knowledge Builder — "modeled as first-class relationship records...
    because a file can participate in many relationships of different kinds
    simultaneously"). Directional in storage (`file_id` → `related_file_id`)
    but conceptually symmetric for every type this phase produces; callers
    query both directions rather than the pipeline writing both, which would
    double the row count for no benefit. `confidence` and `metadata_` let a
    consumer (a future recommendation engine, or just this phase's own
    file-detail UI) explain *why* the edge exists, not just that it does."""

    __tablename__ = "file_relationships"
    __table_args__ = (
        UniqueConstraint(
            "file_id", "related_file_id", "relationship_type", name="uq_file_relationship"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )

    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
