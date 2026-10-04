import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from vault_shared import AIUnavailableError, get_settings
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    ExtractionStatus,
    IntelligenceJobStatus,
    IntelligenceStatus,
    IntelligenceTrigger,
    RoleName,
)
from vault_shared.db.repositories import (
    AIProviderConfigRepository,
    FileExtractionRepository,
    FileIntelligenceRepository,
    FileRepository,
    IntelligenceJobRepository,
    OrganizationRepository,
    RoleRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import encrypt_token
from worker.intelligence.intelligence_service import IntelligenceService


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


class _FakeCompletionProvider:
    """A deterministic stand-in for `OpenAICompatibleCompletionProvider` —
    no real HTTP call, no API key needed. Returns a canned response text
    (JSON by default) and counts calls, mirroring `_FakeEmbeddingProvider`
    in the embedding integration test."""

    name = "fake_completion_provider"

    def __init__(self, *, response_text: str | None = None, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.call_count = 0
        self._response_text = response_text or (
            '{"document_type": "report", "summary": "A short report.", '
            '"entities": [{"type": "org", "value": "Acme Corp", "confidence": 0.8}], '
            '"structured_metadata": {"period": "Q3"}, "topics": ["finance"], "confidence": 0.75}'
        )

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        self.call_count += 1
        return CompletionResult(
            text=self._response_text, provider=self.name, model_name=self.model_name, tokens_used=42
        )


class _RaisingCompletionProvider:
    name = "raising_completion_provider"
    model_name = "fake-model"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or ValueError("simulated completion failure")

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        raise self._error


def _gateway_with_completion(provider: object) -> AIGateway:
    from vault_shared.ai_gateway.providers import LocalEmbeddingProvider

    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=provider,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
    )


def _stub_gateway() -> AIGateway:
    from vault_shared.ai_gateway.providers import LocalEmbeddingProvider

    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=ExtractiveCompletionProvider(),
    )


def _provision_user(db: Session):
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
    db.commit()
    return user


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    db.commit()
    return connector


def _provision_extracted_file(
    db: Session, *, connector_id: uuid.UUID, name: str, provider_file_id: str, text: str | None
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id, provider_drive_id="root", name="My Drive", drive_type=DriveType.MY_DRIVE
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=provider_file_id,
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=len(text or ""),
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
        status=ExtractionStatus.SUCCESS if text is not None else ExtractionStatus.UNSUPPORTED,
        extractor_name="plain_text" if text is not None else None,
        extracted_text=text,
        char_count=len(text) if text is not None else None,
        error=None,
        extracted_at=now,
    )
    db.commit()
    return file


def _create_job(db: Session, *, connector_id: uuid.UUID):
    job = IntelligenceJobRepository(db).create(
        connector_id=connector_id,
        triggered_by=IntelligenceTrigger.MANUAL,
        triggered_by_user_id=None,
    )
    db.commit()
    return job


@requires_infra
def test_analyzes_a_pending_file_and_persists_structured_output(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_extracted_file(
        db,
        connector_id=connector.id,
        name="Q3 Report.txt",
        provider_file_id="f-1",
        text="Acme Corp had a strong Q3.",
    )
    job = _create_job(db, connector_id=connector.id)
    provider = _FakeCompletionProvider()
    service = IntelligenceService(db, ai_gateway=_gateway_with_completion(provider))

    service.run(job.id)

    completed_job = IntelligenceJobRepository(db).get_by_id(job.id)
    assert completed_job.status == IntelligenceJobStatus.COMPLETED
    assert provider.call_count == 1

    intelligence = FileIntelligenceRepository(db).get_by_file_id(file.id)
    assert intelligence is not None
    assert intelligence.status == IntelligenceStatus.SUCCESS
    assert intelligence.document_type == "report"
    assert intelligence.summary == "A short report."
    assert intelligence.entities == [{"type": "org", "value": "Acme Corp", "confidence": 0.8}]
    assert intelligence.structured_metadata == {"period": "Q3"}
    assert intelligence.topics == ["finance"]
    assert intelligence.confidence == 0.75
    assert intelligence.provider == "fake_completion_provider"
    assert intelligence.model_name == "fake-model"


@requires_infra
def test_a_file_with_no_extracted_text_is_marked_unsupported_without_calling_the_provider(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_extracted_file(
        db, connector_id=connector.id, name="Image.png", provider_file_id="f-img", text=None
    )
    job = _create_job(db, connector_id=connector.id)
    provider = _FakeCompletionProvider()
    service = IntelligenceService(db, ai_gateway=_gateway_with_completion(provider))

    service.run(job.id)

    # `list_pending_intelligence_for_connector` filters to
    # `ExtractionStatus.SUCCESS` files only, so this file is never even
    # picked up as pending — the provider is never called, and no
    # FileIntelligence row is created for it. This is the documented
    # defensive branch inside `_analyze_file` guarding a race, not the
    # normal path for genuinely unsupported files.
    assert provider.call_count == 0
    intelligence = FileIntelligenceRepository(db).get_by_file_id(file.id)
    assert intelligence is None
    completed_job = IntelligenceJobRepository(db).get_by_id(job.id)
    assert completed_job.status == IntelligenceJobStatus.COMPLETED


@requires_infra
def test_a_malformed_llm_response_is_recorded_as_a_failed_result_not_a_job_crash(
    db: Session,
) -> None:
    """An unusable response is an anticipated, structured outcome — the
    same treatment `ContentExtractionService` gives an unreadable PDF
    (`FileExtraction.status = FAILED`, not a raised exception) — so it
    counts as *processed*, with the failure visible on the
    `FileIntelligence` row itself, not as a `progress.files_failed`/job
    event (that accounting is reserved for a genuinely unexpected crash —
    see `test_an_unexpected_provider_exception_fails_only_that_file_not_the_job`)."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_extracted_file(
        db, connector_id=connector.id, name="Weird.txt", provider_file_id="f-1", text="some text"
    )
    job = _create_job(db, connector_id=connector.id)
    provider = _FakeCompletionProvider(response_text="I cannot help with that request.")
    service = IntelligenceService(db, ai_gateway=_gateway_with_completion(provider))

    service.run(job.id)

    completed_job = IntelligenceJobRepository(db).get_by_id(job.id)
    assert completed_job.status == IntelligenceJobStatus.COMPLETED
    assert completed_job.progress.files_processed == 1
    assert completed_job.progress.files_failed == 0

    intelligence = FileIntelligenceRepository(db).get_by_file_id(file.id)
    assert intelligence is not None
    assert intelligence.status == IntelligenceStatus.FAILED
    assert intelligence.error is not None


def _run_with_provider_error(db: Session, error: Exception) -> tuple[uuid.UUID, Exception | None]:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_extracted_file(
        db, connector_id=connector.id, name="Good.txt", provider_file_id="f-good", text="good text"
    )
    job = _create_job(db, connector_id=connector.id)
    service = IntelligenceService(
        db, ai_gateway=_gateway_with_completion(_RaisingCompletionProvider(error))
    )
    try:
        service.run(job.id)
    except Exception as raised:  # noqa: BLE001 - the test inspects whatever the job boundary raises
        return job.id, raised
    return job.id, None


@requires_infra
def test_an_unusable_ai_response_fails_only_that_file_not_the_job(db: Session) -> None:
    job_id, raised = _run_with_provider_error(
        db,
        AIUnavailableError("bad body", reason=AIUnavailableError.INVALID_RESPONSE),
    )

    assert raised is None
    job = IntelligenceJobRepository(db).get_by_id(job_id)
    assert job.status == IntelligenceJobStatus.COMPLETED
    assert job.progress.files_failed == 1
    assert job.progress.files_processed == 0


@requires_infra
def test_an_unexpected_provider_exception_is_retried_as_a_whole_job_condition(db: Session) -> None:
    job_id, raised = _run_with_provider_error(db, ValueError("simulated completion failure"))

    assert isinstance(raised, AIUnavailableError)
    assert raised.reason == AIUnavailableError.PROVIDER_ERROR
    assert raised.retryable is True
    job = IntelligenceJobRepository(db).get_by_id(job_id)
    assert job.status != IntelligenceJobStatus.COMPLETED


@requires_infra
def test_a_rejected_ai_key_fails_the_job_without_retry(db: Session) -> None:
    job_id, raised = _run_with_provider_error(
        db, AIUnavailableError("nope", reason=AIUnavailableError.AUTH_FAILED)
    )

    assert raised is None
    job = IntelligenceJobRepository(db).get_by_id(job_id)
    assert job.status == IntelligenceJobStatus.FAILED
    assert AIUnavailableError.AUTH_FAILED in (job.error or "")


@requires_infra
def test_fails_fast_when_only_the_stub_provider_is_configured(db: Session) -> None:
    """The critical job-level guard — never burns through every pending
    file against a provider that can't return JSON, producing a
    misleading "N files failed" history when the real story is "nothing
    is configured yet."."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_extracted_file(
        db, connector_id=connector.id, name="Report.txt", provider_file_id="f-1", text="report body"
    )
    job = _create_job(db, connector_id=connector.id)
    service = IntelligenceService(db, ai_gateway=_stub_gateway())

    service.run(job.id)

    completed_job = IntelligenceJobRepository(db).get_by_id(job.id)
    assert completed_job.status == IntelligenceJobStatus.FAILED
    assert completed_job.error is not None
    assert "No completion provider configured" in completed_job.error

    progress = completed_job.progress
    assert progress.files_processed == 0
    assert progress.files_failed == 0
    assert progress.files_pending == 0


@requires_infra
def test_run_uses_the_organizations_own_ai_provider_when_configured(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard for the `with_completion_provider()` rebind in
    `run()` — the service is built with the *stub* gateway (the same
    stand-in as an instance with no `.env` completion config), which on
    its own would fail this job fast at the `completion_provider_name ==
    _STUB_PROVIDER_NAME` gate. If the org's own key isn't resolved and
    rebound *before* that gate runs, this job fails instead of
    completing."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", key)
    get_settings.cache_clear()

    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_extracted_file(
        db,
        connector_id=connector.id,
        name="Q3 Report.txt",
        provider_file_id="f-org",
        text="Acme Corp had a strong Q3.",
    )
    AIProviderConfigRepository(db).upsert(
        organization_id=user.organization_id,
        api_key_encrypted=encrypt_token("sk-or-v1-org-key"),
        model_name="z-ai/glm-5.2:free",
    )
    db.commit()
    job = _create_job(db, connector_id=connector.id)

    response_text = (
        '{"document_type": "report", "summary": "A short report.", '
        '"entities": [], "structured_metadata": {}, "topics": [], "confidence": 0.75}'
    )

    def _fake_complete(
        self: OpenAICompatibleCompletionProvider, *, messages: object, context: object, max_tokens: int
    ) -> CompletionResult:
        return CompletionResult(
            text=response_text, provider=self.name, model_name=self.model_name, tokens_used=1
        )

    monkeypatch.setattr(OpenAICompatibleCompletionProvider, "complete", _fake_complete)

    service = IntelligenceService(db, ai_gateway=_stub_gateway())

    service.run(job.id)

    completed_job = IntelligenceJobRepository(db).get_by_id(job.id)
    assert completed_job.status == IntelligenceJobStatus.COMPLETED

    intelligence = FileIntelligenceRepository(db).get_by_file_id(file.id)
    assert intelligence is not None
    assert intelligence.status == IntelligenceStatus.SUCCESS
    assert intelligence.provider == "openai_compatible"
    assert intelligence.model_name == "z-ai/glm-5.2:free"

    get_settings.cache_clear()


@requires_infra
def test_cancellation_stops_the_job_cleanly(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_extracted_file(
        db, connector_id=connector.id, name="Report.txt", provider_file_id="f-1", text="report body"
    )
    job = _create_job(db, connector_id=connector.id)
    jobs = IntelligenceJobRepository(db)
    jobs.request_cancel(job)
    db.commit()

    service = IntelligenceService(db, ai_gateway=_gateway_with_completion(_FakeCompletionProvider()))

    service.run(job.id)

    cancelled_job = jobs.get_by_id(job.id)
    assert cancelled_job.status == IntelligenceJobStatus.CANCELLED


@requires_infra
def test_a_provider_or_model_change_makes_every_file_pending_again(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_extracted_file(
        db, connector_id=connector.id, name="Report.txt", provider_file_id="f-1", text="report body"
    )
    provider_v1 = _FakeCompletionProvider(model_name="model-v1")
    service_v1 = IntelligenceService(db, ai_gateway=_gateway_with_completion(provider_v1))
    first_job = _create_job(db, connector_id=connector.id)
    service_v1.run(first_job.id)
    assert provider_v1.call_count == 1

    # Re-running immediately with the SAME model must not re-call the
    # provider — nothing changed since the last analysis.
    second_job = _create_job(db, connector_id=connector.id)
    service_v1.run(second_job.id)
    assert provider_v1.call_count == 1

    # A model swap (e.g. Kimi to a different model) must treat every
    # already-analyzed file as pending again.
    provider_v2 = _FakeCompletionProvider(model_name="model-v2")
    service_v2 = IntelligenceService(db, ai_gateway=_gateway_with_completion(provider_v2))
    third_job = _create_job(db, connector_id=connector.id)
    service_v2.run(third_job.id)
    assert provider_v2.call_count == 1
