import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vault_shared.db.session import Base

if TYPE_CHECKING:
    from vault_shared.db.models.conversation_message import ConversationMessage


class Citation(Base):
    """A structured source reference for one assistant message (Phase 6
    spec's "Citation System") — kept as its own queryable table, not a
    JSON blob on the message, so a citation can be joined back to the
    `File` it names. `retrieval_method` records *how* this file was found
    (metadata search, semantic search, or both) — the explainability the
    phase spec asks for isn't just "here's a file," it's "here's why this
    file, found by which method, with what confidence." For an answer drawn
    from file content, `page_number`/`passage_index` say exactly where."""

    __tablename__ = "citations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snippet: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    retrieval_method: Mapped[str] = mapped_column(String(20), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passage_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["ConversationMessage"] = relationship(back_populates="citations")
