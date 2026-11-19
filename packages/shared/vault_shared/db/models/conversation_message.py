import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vault_shared.db.session import Base

if TYPE_CHECKING:
    from vault_shared.db.models.citation import Citation
    from vault_shared.db.models.conversation import Conversation


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(Base):
    """One turn in a `Conversation`. Only `ASSISTANT` messages carry
    `retrieval_method`/`provider`/`token_usage`/`tool_name`/`citations` — a
    `USER` message is just the question as asked. `provider` records which
    AI Gateway adapter actually answered (Phase 6 spec's "AI providers
    remain fully interchangeable" — this is what proves it, per-message,
    rather than assuming from config). `tool_name` is separate from
    `retrieval_method` (String(20), too narrow for names like
    "get_storage_statistics") — set only when the AI Storage Assistant's
    deterministic intent classifier routed this turn to a tool instead of
    the semantic-search RAG path."""

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    token_usage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Structured answer parts — file lists, duplicate groups, action
    # proposals/results — rendered by the client next to `content`.
    blocks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
