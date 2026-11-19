import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class FileLifecycleState(enum.StrEnum):
    """Spec's lifecycle states — "Never equate old = delete." `UNKNOWN` is a
    real, reachable state (no signal at all), not an error."""

    KEEP = "keep"
    ACTIVE = "active"
    REFERENCE = "reference"
    ARCHIVE_CANDIDATE = "archive_candidate"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    OBSOLETE_CANDIDATE = "obsolete_candidate"
    REVIEW_REQUIRED = "review_required"
    UNKNOWN = "unknown"


class FileLifecycle(Base):
    """The Lifecycle Intelligence Layer's one-row-per-file judgment — same
    PK=`file_id` convention as `FileMetadata`/`FileClassification`/
    `Embedding`. Purely deterministic (see `worker.organization.
    lifecycle_scorer`): reads already-computed `FileRelationship`/
    `DuplicateGroupMember`/`FileEntityLink`/`File` timestamps, never calls
    AI, so it keeps working with GLM disabled (spec's AI-outage
    requirement) by construction. `evidence` is the human-readable signal
    list the "Why?" view renders; `signals` is the raw scoring input dict,
    kept separately for debugging/explainability without cluttering the
    evidence prose."""

    __tablename__ = "file_lifecycles"

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )

    state: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    scorer_version: Mapped[str] = mapped_column(String(20), nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
