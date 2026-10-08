import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.application.conversation_service import AssistantTurn, ConversationDetail
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_conversation_service
from fastapi.testclient import TestClient
from vault_shared import NotFoundError

client = TestClient(app)


class _FakeConversation:
    def __init__(self, *, title: str | None = "What files do we have?") -> None:
        self.id = uuid.uuid4()
        self.title = title
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class _FakeMessage:
    def __init__(self, *, role: str, content: str) -> None:
        self.id = uuid.uuid4()
        self.role = role
        self.content = content
        self.retrieval_method = "semantic" if role == "assistant" else None
        self.provider = "extractive_fallback" if role == "assistant" else None
        self.token_usage = None
        self.tool_name = None
        self.blocks = []
        self.created_at = datetime.now(UTC)


class _FakeCitation:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.file_id = uuid.uuid4()
        self.snippet = "Relevant excerpt…"
        self.confidence = 0.72
        self.retrieval_method = "semantic"
        self.page_number = 3
        self.passage_index = 7


class _FakeFile:
    def __init__(self, *, id: uuid.UUID) -> None:
        self.id = id
        self.name = "Payroll.pdf"
        self.size_bytes = 204800
        self.mime_type = "application/pdf"


@pytest.fixture
def fake_conversation_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_conversation_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_conversation_service, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_list_conversations_requires_authentication(fake_conversation_service) -> None:
    response = client.get("/v1/conversations")
    assert response.status_code == 401


def test_list_conversations_returns_the_users_conversations(as_member, fake_conversation_service) -> None:
    fake_conversation_service.list_for_user.return_value = [_FakeConversation()]

    response = client.get("/v1/conversations")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_start_conversation_creates_the_first_turn(as_member, fake_conversation_service) -> None:
    conversation = _FakeConversation()
    user_message = _FakeMessage(role="user", content="What files do we have about payroll?")
    assistant_message = _FakeMessage(role="assistant", content="Here's what I found…")
    citation = _FakeCitation()
    file = _FakeFile(id=citation.file_id)
    fake_conversation_service.ask.return_value = AssistantTurn(
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
        citations=[citation],
        files_by_id={file.id: file},
    )

    response = client.post(
        "/v1/conversations", json={"question": "What files do we have about payroll?"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["conversation"]["id"] == str(conversation.id)
    assert body["assistant_message"]["content"] == "Here's what I found…"
    assert len(body["assistant_message"]["citations"]) == 1
    assert body["assistant_message"]["citations"][0]["file_id"] == str(citation.file_id)
    assert body["assistant_message"]["citations"][0]["file_name"] == "Payroll.pdf"
    assert body["assistant_message"]["citations"][0]["file_size_bytes"] == 204800
    call_kwargs = fake_conversation_service.ask.call_args.kwargs
    assert call_kwargs["conversation_id"] is None


def test_start_conversation_rejects_an_empty_question(as_member, fake_conversation_service) -> None:
    response = client.post("/v1/conversations", json={"question": ""})

    assert response.status_code == 422
    fake_conversation_service.ask.assert_not_called()


def test_send_message_continues_an_existing_conversation(as_member, fake_conversation_service) -> None:
    conversation = _FakeConversation()
    fake_conversation_service.ask.return_value = AssistantTurn(
        conversation=conversation,
        user_message=_FakeMessage(role="user", content="And last quarter?"),
        assistant_message=_FakeMessage(role="assistant", content="Last quarter…"),
        citations=[],
        files_by_id={},
    )

    response = client.post(
        f"/v1/conversations/{conversation.id}/messages", json={"question": "And last quarter?"}
    )

    assert response.status_code == 201
    call_kwargs = fake_conversation_service.ask.call_args.kwargs
    assert call_kwargs["conversation_id"] == conversation.id


def test_send_message_returns_not_found_for_another_users_conversation(
    as_member, fake_conversation_service
) -> None:
    fake_conversation_service.ask.side_effect = NotFoundError("Conversation not found.")

    response = client.post(
        f"/v1/conversations/{uuid.uuid4()}/messages", json={"question": "Anything?"}
    )

    assert response.status_code == 404


def test_get_conversation_includes_messages_and_citations(as_member, fake_conversation_service) -> None:
    conversation = _FakeConversation()
    assistant_message = _FakeMessage(role="assistant", content="Here's what I found…")
    citation = _FakeCitation()
    file = _FakeFile(id=citation.file_id)
    fake_conversation_service.get_detail.return_value = ConversationDetail(
        conversation=conversation,
        messages=[assistant_message],
        citations_by_message_id={assistant_message.id: [citation]},
        files_by_id={file.id: file},
    )

    response = client.get(f"/v1/conversations/{conversation.id}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["messages"]) == 1
    assert len(body["messages"][0]["citations"]) == 1


def test_get_conversation_returns_not_found_for_another_users_conversation(
    as_member, fake_conversation_service
) -> None:
    fake_conversation_service.get_detail.side_effect = NotFoundError("Conversation not found.")

    response = client.get(f"/v1/conversations/{uuid.uuid4()}")

    assert response.status_code == 404
