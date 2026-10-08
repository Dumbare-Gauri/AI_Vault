"""Coverage for `resolve_org_completion_provider` — the resolver behind
per-organization AI provider configuration. Needs a real DB (the config
row lookup), so lives in `integration/`, not `unit/`, despite testing a
`packages/shared` function."""

import socket
import uuid
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from vault_shared import get_settings
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)
from vault_shared.db.repositories import (
    AIProviderConfigRepository,
    OrganizationRepository,
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


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


def _provision_org(db: Session):
    unique = uuid.uuid4().hex[:12]
    organization = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
    db.commit()
    return organization


@requires_infra
def test_returns_none_for_an_organization_with_no_config(db: Session, encryption_key) -> None:
    org = _provision_org(db)

    result = resolve_org_completion_provider(db, org.id)

    assert result is None


@requires_infra
def test_returns_a_configured_provider_for_an_organization_with_a_key(
    db: Session, encryption_key
) -> None:
    org = _provision_org(db)
    AIProviderConfigRepository(db).upsert(
        provider="openrouter",
        organization_id=org.id,
        api_key_encrypted=encrypt_token("sk-or-v1-test-key"),
        model_name="z-ai/glm-5.2:free",
    )
    db.commit()

    result = resolve_org_completion_provider(db, org.id)

    assert isinstance(result, OpenAICompatibleCompletionProvider)
    assert result.model_name == "z-ai/glm-5.2:free"


@requires_infra
def test_returns_none_gracefully_when_the_stored_key_cannot_be_decrypted(
    db: Session, encryption_key, monkeypatch
) -> None:
    org = _provision_org(db)
    AIProviderConfigRepository(db).upsert(
        provider="openrouter",
        organization_id=org.id,
        api_key_encrypted=encrypt_token("sk-or-v1-test-key"),
        model_name="z-ai/glm-5.2:free",
    )
    db.commit()

    # Rotate the encryption key out from under the stored ciphertext.
    other_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", other_key)
    get_settings.cache_clear()

    result = resolve_org_completion_provider(db, org.id)

    assert result is None
