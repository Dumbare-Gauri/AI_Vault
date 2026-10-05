"""Coverage for `AIProviderConfigService`'s blank-key-keeps-existing-key
semantics — the one behavior genuinely worth a real-DB test, since it's
the difference between "update the model" and "accidentally wipe a saved
key" for an admin who only meant to change the model."""

import socket
import uuid
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.application.ai_provider_config_service import AIProviderConfigService
from vault_shared import ValidationError, get_settings
from vault_shared.db.repositories import OrganizationRepository
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import decrypt_token


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
def test_setting_a_new_org_requires_an_api_key(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)

    with pytest.raises(ValidationError):
        service.set(org.id, api_key=None, model_name="z-ai/glm-5.2:free", provider="openrouter")


@requires_infra
def test_a_blank_api_key_on_update_keeps_the_existing_key(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)
    service.set(
        org.id, api_key="sk-or-v1-original-key", model_name="z-ai/glm-4.7", provider="openrouter"
    )

    updated = service.set(
        org.id, api_key=None, model_name="z-ai/glm-5.2:free", provider="openrouter"
    )

    assert updated.model_name == "z-ai/glm-5.2:free"
    assert decrypt_token(updated.api_key_encrypted) == "sk-or-v1-original-key"


@requires_infra
def test_a_new_api_key_on_update_replaces_the_existing_key(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)
    service.set(
        org.id, api_key="sk-or-v1-original-key", model_name="z-ai/glm-4.7", provider="openrouter"
    )

    updated = service.set(
        org.id, api_key="sk-or-v1-new-key", model_name="z-ai/glm-4.7", provider="openrouter"
    )

    assert decrypt_token(updated.api_key_encrypted) == "sk-or-v1-new-key"


@requires_infra
def test_get_status_reflects_what_was_set(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)

    assert service.get_status(org.id) is None

    service.set(
        org.id, api_key="sk-or-v1-test", model_name="z-ai/glm-5.2:free", provider="openrouter"
    )

    status = service.get_status(org.id)
    assert status is not None
    assert status.model_name == "z-ai/glm-5.2:free"


@requires_infra
def test_clear_removes_the_config(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)
    service.set(
        org.id, api_key="sk-or-v1-test", model_name="z-ai/glm-5.2:free", provider="openrouter"
    )

    service.clear(org.id)

    assert service.get_status(org.id) is None


@requires_infra
def test_clear_on_an_unconfigured_org_is_a_no_op(db: Session, encryption_key) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)

    service.clear(org.id)  # must not raise

    assert service.get_status(org.id) is None


@requires_infra
def test_test_with_no_stored_key_and_no_candidate_key_reports_failure(
    db: Session, encryption_key
) -> None:
    org = _provision_org(db)
    service = AIProviderConfigService(db)

    success, error = service.test(
        org.id, api_key=None, model_name="z-ai/glm-5.2:free", provider="openrouter"
    )

    assert success is False
    assert error is not None
