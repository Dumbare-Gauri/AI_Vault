import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from app.application.auth_service import AuthService
from app.application.conversation_service import ConversationService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from vault_shared import NotFoundError, get_settings
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.interfaces import CompletionResult, EmbeddingResult
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)
from vault_shared.db.models import ConnectorProvider, DriveType
from vault_shared.db.repositories import (
    AIProviderConfigRepository,
    EmbeddingRepository,
    FileExtractionRepository,
    FileRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import encrypt_token


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except OSError:
        return False


def _infra_available() -> bool:
    settings = get_settings()
    return _reachable(settings.database_url) and _reachable(settings.redis_url)


requires_infra = pytest.mark.skipif(
    not _infra_available(),
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI service containers.",
)


class _FixedVectorEmbeddingProvider:
    name = "fixed_vector_test_double"
    model_name = "test"
    model_version = "1"

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [
            EmbeddingResult(
                vector=self._vector,
                model_name=self.model_name,
                model_version=self.model_version,
                dimensions=len(self._vector),
            )
            for _ in texts
        ]


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _provision_user(db: Session):
    unique = uuid.uuid4().hex[:12]
    google_user = GoogleUserInfo(
        sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        email_verified=True,
        name="Ada Founder",
        picture=None,
    )
    session = AuthService(db).complete_google_login(google_user=google_user, ip_address=None)
    return session.user


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    return StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )


def _provision_embedded_file(
    db: Session,
    *,
    connector_id: uuid.UUID,
    name: str,
    text: str,
    vector: list[float] | None = None,
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id, provider_drive_id="root", name="My Drive", drive_type=DriveType.MY_DRIVE
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=str(uuid.uuid4()),
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="application/pdf",
        size_bytes=1024,
        owner_email="founder@acme.com",
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    FileExtractionRepository(db).upsert(
        file_id=file.id,
        status="success",
        extractor_name="pdf_text",
        extracted_text=text,
        char_count=len(text),
        error=None,
        extracted_at=now,
    )
    EmbeddingRepository(db).upsert(
        file_id=file.id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=vector or [1.0, 0.0, 0.0, 0.0],
        content_hash=f"hash-{file.id}",
        embedded_at=now,
    )
    db.commit()
    return file


def _gateway() -> AIGateway:
    return AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )


@requires_infra
def test_ask_creates_a_new_conversation_with_a_derived_title(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_embedded_file(
        db, connector_id=connector.id, name="Payroll.pdf", text="Payroll figures for Q3."
    )
    # A second, unrelated file with a distinct vector — a single-file
    # candidate pool identical to the query is a degenerate case for
    # `rank_by_similarity`'s mean-centering (see test_search_service_
    # integration.py's identical comment); a real organization always has
    # more than one embedded file.
    _provision_embedded_file(
        db,
        connector_id=connector.id,
        name="Unrelated.pdf",
        text="Completely unrelated content.",
        vector=[0.0, 1.0, 0.0, 0.0],
    )
    service = ConversationService(db, ai_gateway=_gateway())

    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question="What does the payroll file say?",
    )

    assert turn.conversation.title == "What does the payroll file say?"
    assert turn.user_message.role == "user"
    assert turn.assistant_message.role == "assistant"
    assert turn.assistant_message.provider == "extractive_fallback"
    assert len(turn.citations) == 1
    assert "Payroll figures for Q3." in turn.assistant_message.content


@requires_infra
def test_ask_with_no_matching_context_uses_the_no_context_fallback(db: Session) -> None:
    user = _provision_user(db)
    service = ConversationService(db, ai_gateway=_gateway())

    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question="Anything at all?",
    )

    assert turn.citations == []
    assert "don't have any relevant documents" in turn.assistant_message.content


@requires_infra
def test_a_follow_up_question_continues_the_same_conversation(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_embedded_file(
        db, connector_id=connector.id, name="Payroll.pdf", text="Payroll figures for Q3."
    )
    service = ConversationService(db, ai_gateway=_gateway())
    first_turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question="What does the payroll file say?",
    )

    second_turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=first_turn.conversation.id,
        question="Tell me more.",
    )

    assert second_turn.conversation.id == first_turn.conversation.id
    detail = service.get_detail(
        first_turn.conversation.id, organization_id=user.organization_id, user_id=user.id
    )
    assert len(detail.messages) == 4


@requires_infra
def test_ask_uses_the_organizations_own_ai_provider_when_configured(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard for `AIGateway.with_completion_provider()` —
    without it, an org's own key would be silently ignored whenever the
    *global* gateway (constructed here wrapping the stub, exactly like an
    instance with no `.env` completion config) is what `ConversationService`
    was built with, since `_answer_with_search` reads whichever `ai_gateway`
    `ask()` passes it, not `self._ai_gateway` directly."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", key)
    get_settings.cache_clear()

    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_embedded_file(
        db, connector_id=connector.id, name="Payroll.pdf", text="Payroll figures for Q3."
    )
    AIProviderConfigRepository(db).upsert(
        provider="openrouter",
        organization_id=user.organization_id,
        api_key_encrypted=encrypt_token("sk-or-v1-org-key"),
        model_name="z-ai/glm-5.2:free",
    )
    db.commit()

    def _fake_complete(
        self: OpenAICompatibleCompletionProvider, *, messages: object, context: object, max_tokens: int
    ) -> CompletionResult:
        return CompletionResult(
            text="org-provider answer", provider=self.name, model_name=self.model_name, tokens_used=1
        )

    monkeypatch.setattr(OpenAICompatibleCompletionProvider, "complete", _fake_complete)

    # The service is built with the GLOBAL gateway wrapping the stub — if
    # the org's own provider is never resolved/rebound, `ask()` would fall
    # through to the stub's deterministic answer instead.
    global_gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    service = ConversationService(db, ai_gateway=global_gateway)

    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question="What does the payroll file say?",
    )

    assert turn.assistant_message.provider == "openai_compatible"
    assert turn.assistant_message.content == "org-provider answer"

    get_settings.cache_clear()


@requires_infra
def test_get_detail_rejects_another_users_conversation(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    service = ConversationService(db, ai_gateway=_gateway())
    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question="Anything?",
    )

    with pytest.raises(NotFoundError):
        service.get_detail(
            turn.conversation.id, organization_id=other_user.organization_id, user_id=other_user.id
        )
