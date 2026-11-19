import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import Conversation


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None,
        connector_id: uuid.UUID | None = None,
    ) -> Conversation:
        conversation = Conversation(
            organization_id=organization_id, user_id=user_id, title=title, connector_id=connector_id
        )
        self._session.add(conversation)
        self._session.flush()
        return conversation

    def get_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        return self._session.get(Conversation, conversation_id)

    def get_owned(
        self, conversation_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> Conversation | None:
        """Conversations are user-scoped, not just organization-scoped
        (Phase 6 spec: "User-specific conversation history") — one
        organization member never sees another's chat history."""
        return (
            self._session.query(Conversation)
            .filter_by(id=conversation_id, organization_id=organization_id, user_id=user_id)
            .first()
        )

    def list_for_user(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        connector_id: uuid.UUID | None = None,
    ) -> list[Conversation]:
        query = self._session.query(Conversation).filter_by(
            organization_id=organization_id, user_id=user_id
        )
        if connector_id is not None:
            query = query.filter(Conversation.connector_id == connector_id)
        return query.order_by(Conversation.updated_at.desc()).all()

    def touch(self, conversation: Conversation) -> None:
        """Bumps `updated_at` so the conversation list can sort by most
        recent activity — called after appending a new message. Set
        explicitly rather than relying on `onupdate`, since appending a
        message doesn't itself change any column on this row."""
        conversation.updated_at = datetime.now(UTC)
        self._session.flush()
