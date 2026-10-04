"""Integration coverage for `ConversationService.ask()`'s tool-routed path
(ADR-024) — persona placement, `tool_name` persistence, that a tool turn
skips `SearchService` entirely, graceful rate-limit degradation, bounded
history on both branches, and the deterministic-formatter fallback when no
real completion provider is configured."""

import socket
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from app.application.conversation_service import _DEGRADED_NOTICE, ConversationService
from vault_shared import AIUnavailableError, get_settings
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider, LocalEmbeddingProvider
from vault_shared.db.models import ConnectorProvider, RoleName, StorageAnalysisTrigger
from vault_shared.db.repositories import (
    OrganizationRepository,
    RoleRepository,
    SearchSessionRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory


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


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _RecordingCompletionProvider:
    """Captures every `complete()` call's `messages`/`context` so a test
    can assert message ordering and content without a real network call —
    same role `_FixedVectorEmbeddingProvider` plays for embeddings in
    `test_conversation_service_integration.py`."""

    name = "recording_test_double"
    model_name = "test"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        self.calls.append({"messages": messages, "context": context, "max_tokens": max_tokens})
        return CompletionResult(
            text="a recorded answer", provider=self.name, model_name=self.model_name, tokens_used=10
        )


def _gateway(completion_provider: object) -> AIGateway:
    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=completion_provider,  # type: ignore[arg-type]
    )


def _stub_gateway() -> AIGateway:
    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=ExtractiveCompletionProvider(),
    )


def _provision_org(db: Session):
    unique = uuid.uuid4().hex[:12]
    organization = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
    role = RoleRepository(db).get_by_name(RoleName.OWNER)
    user = UserRepository(db).create(
        organization_id=organization.id,
        role_id=role.id,
        google_sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        name="Ada Founder",
        avatar_url=None,
    )
    StorageConnectorRepository(db).upsert_connected(
        organization_id=organization.id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user.id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    db.commit()
    return organization, user


def _seed_snapshot(db: Session, *, organization_id: uuid.UUID):
    job = StorageAnalysisJobRepository(db).create(
        organization_id=organization_id,
        triggered_by=StorageAnalysisTrigger.MANUAL,
        triggered_by_user_id=None,
    )
    StorageAnalysisJobRepository(db).mark_completed(job)
    snapshot = StorageAnalysisSnapshotRepository(db).create(
        organization_id=organization_id,
        storage_analysis_job_id=job.id,
        total_size_bytes=10_240,
        total_files=5,
        total_folders=1,
        breakdown_by_type_bytes={},
        breakdown_by_size_bucket_bytes={},
        breakdown_by_source_bytes={},
        duplicate_group_count=2,
        duplicate_file_count=4,
        duplicate_recoverable_bytes=1_024,
        large_file_count=1,
        large_file_bytes=8_192,
        old_file_count=0,
        old_file_bytes=0,
        inactive_file_count=0,
        inactive_file_bytes=0,
        temporary_candidate_count=0,
        temporary_candidate_bytes=0,
        total_potential_savings_bytes=1_024,
    )
    db.commit()
    return snapshot


@requires_infra
def test_a_tool_turn_places_the_persona_before_any_data_context(db: Session) -> None:
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    provider = _RecordingCompletionProvider()
    service = ConversationService(db, ai_gateway=_gateway(provider))

    service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    assert len(provider.calls) == 1
    system_messages = [m for m in provider.calls[0]["messages"] if m.role == "system"]
    assert system_messages
    assert "Storage Assistant" in system_messages[0].content


@requires_infra
def test_a_tool_turn_persists_the_tool_name(db: Session) -> None:
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    service = ConversationService(db, ai_gateway=_gateway(_RecordingCompletionProvider()))

    turn = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    assert turn.assistant_message.tool_name == "get_storage_overview"
    assert turn.assistant_message.retrieval_method == "tool"


@requires_infra
def test_a_tool_turn_never_calls_search_service(db: Session) -> None:
    """A pure-arithmetic storage question must not run a semantic search
    over file content — a clean, observable proxy for "the RAG path was
    skipped": no new SearchSession row."""
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    before = len(SearchSessionRepository(db).list_for_user(organization_id=org.id, user_id=user.id))
    service = ConversationService(db, ai_gateway=_gateway(_RecordingCompletionProvider()))

    service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    after = len(SearchSessionRepository(db).list_for_user(organization_id=org.id, user_id=user.id))
    assert after == before


@requires_infra
def test_a_rag_turn_also_gets_the_persona_prompt(db: Session) -> None:
    org, user = _provision_org(db)
    provider = _RecordingCompletionProvider()
    service = ConversationService(db, ai_gateway=_gateway(provider))

    service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="What does the payroll file say?",
    )

    system_messages = [m for m in provider.calls[0]["messages"] if m.role == "system"]
    assert system_messages
    assert "Storage Assistant" in system_messages[0].content


@requires_infra
def test_rate_limit_exceeded_degrades_to_the_rag_path(db: Session) -> None:
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    provider = _RecordingCompletionProvider()
    service = ConversationService(db, ai_gateway=_gateway(provider))

    with patch(
        "app.application.conversation_service.check_rate_limit", return_value=False
    ):
        turn = service.ask(
            organization_id=org.id, user_id=user.id, conversation_id=None,
            question="How much storage am I using?",
        )

    assert turn.assistant_message.tool_name is None
    assert turn.assistant_message.retrieval_method != "tool"


@requires_infra
def test_history_is_bounded_on_the_tool_path(db: Session) -> None:
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    provider = _RecordingCompletionProvider()
    service = ConversationService(db, ai_gateway=_gateway(provider))
    settings = get_settings()

    conversation_id = None
    for i in range(settings.ai_max_history_messages + 4):
        turn = service.ask(
            organization_id=org.id, user_id=user.id, conversation_id=conversation_id,
            question=f"How much storage am I using? (turn {i})",
        )
        conversation_id = turn.conversation.id

    last_call_messages = provider.calls[-1]["messages"]
    non_system = [m for m in last_call_messages if m.role != "system"]
    # history (bounded) + the current question
    assert len(non_system) <= settings.ai_max_history_messages + 1


@requires_infra
def test_deterministic_formatter_answers_correctly_with_no_llm_configured(db: Session) -> None:
    org, user = _provision_org(db)
    snapshot = _seed_snapshot(db, organization_id=org.id)
    service = ConversationService(db, ai_gateway=_stub_gateway())

    turn = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    assert turn.assistant_message.tool_name == "get_storage_overview"
    assert "10.0 KB" in turn.assistant_message.content or "10 KB" in turn.assistant_message.content
    assert str(snapshot.total_files) in turn.assistant_message.content


class _UnavailableCompletionProvider:
    name = "unavailable_test_double"
    model_name = "test"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        raise AIUnavailableError("simulated outage", reason=self._reason)


@requires_infra
@pytest.mark.parametrize(
    "reason",
    [AIUnavailableError.AUTH_FAILED, AIUnavailableError.TIMEOUT, AIUnavailableError.QUOTA_EXCEEDED],
)
def test_a_tool_turn_still_answers_with_facts_when_the_ai_is_unavailable(
    db: Session, reason: str
) -> None:
    org, user = _provision_org(db)
    snapshot = _seed_snapshot(db, organization_id=org.id)
    service = ConversationService(db, ai_gateway=_gateway(_UnavailableCompletionProvider(reason)))

    turn = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    content = turn.assistant_message.content
    assert content.startswith(_DEGRADED_NOTICE)
    assert str(snapshot.total_files) in content
    assert turn.assistant_message.tool_name == "get_storage_overview"
    assert turn.assistant_message.provider == "extractive_fallback"


@requires_infra
def test_a_search_turn_is_answered_and_persisted_when_the_ai_is_unavailable(db: Session) -> None:
    org, user = _provision_org(db)
    service = ConversationService(
        db, ai_gateway=_gateway(_UnavailableCompletionProvider(AIUnavailableError.UNREACHABLE))
    )

    turn = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="What does the payroll file say?",
    )

    assert turn.assistant_message.content.startswith(_DEGRADED_NOTICE)
    assert turn.user_message.content == "What does the payroll file say?"
    assert turn.conversation.id is not None


@requires_infra
def test_the_conversation_can_continue_after_an_ai_outage(db: Session) -> None:
    org, user = _provision_org(db)
    _seed_snapshot(db, organization_id=org.id)
    service = ConversationService(
        db, ai_gateway=_gateway(_UnavailableCompletionProvider(AIUnavailableError.TIMEOUT))
    )
    first = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None,
        question="How much storage am I using?",
    )

    second = service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=first.conversation.id,
        question="How much storage am I using now?",
    )

    assert second.conversation.id == first.conversation.id
