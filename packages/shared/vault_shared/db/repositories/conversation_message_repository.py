import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import ConversationMessage


class ConversationMessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        retrieval_method: str | None = None,
        provider: str | None = None,
        token_usage: int | None = None,
        tool_name: str | None = None,
        blocks: list | None = None,
    ) -> ConversationMessage:
        message = ConversationMessage(
            # Stamped now, not at transaction start: a question and its answer
            # are saved in one transaction and must still read back in order.
            created_at=datetime.now(UTC),
            blocks=blocks or [],
            conversation_id=conversation_id,
            role=role,
            content=content,
            retrieval_method=retrieval_method,
            provider=provider,
            token_usage=token_usage,
            tool_name=tool_name,
        )
        self._session.add(message)
        self._session.flush()
        return message

    def list_for_conversation(self, conversation_id: uuid.UUID) -> list[ConversationMessage]:
        return (
            self._session.query(ConversationMessage)
            .filter_by(conversation_id=conversation_id)
            .order_by(ConversationMessage.created_at, ConversationMessage.role.desc())
            .all()
        )

    def list_recent_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int
    ) -> list[ConversationMessage]:
        """Bounded history for `ConversationService.ask()`'s completion
        call — a separate method from `list_for_conversation` (not an
        optional `limit` param on it) so the thread page's `get_detail`,
        which needs the *full* transcript, can never be accidentally
        truncated by a caller passing the wrong default."""
        rows = (
            self._session.query(ConversationMessage)
            .filter_by(conversation_id=conversation_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.role)
            .limit(limit)
            .all()
        )
        return list(reversed(rows))
