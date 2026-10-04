from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.application.ai_status_service import get_ai_status
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from vault_shared.ai_gateway import get_ai_gateway
from vault_shared.db.session import get_db

client = TestClient(app)


def _gateway(provider_name: str, model_name: str = "stub") -> MagicMock:
    gateway = MagicMock()
    gateway.completion_provider_name = provider_name
    gateway.completion_model_name = model_name
    return gateway


def _org_provider(model_name: str = "z-ai/glm-test") -> MagicMock:
    provider = MagicMock()
    provider.model_name = model_name
    return provider


def _get_status(owner_user, gateway: MagicMock, org_provider: MagicMock | None):
    app.dependency_overrides[get_current_user] = lambda: owner_user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_ai_gateway] = lambda: gateway
    try:
        with patch(
            "app.application.ai_status_service.resolve_org_completion_provider",
            return_value=org_provider,
        ):
            return client.get("/v1/ai/status")
    finally:
        for dependency in (get_current_user, get_db, get_ai_gateway):
            app.dependency_overrides.pop(dependency, None)


def test_status_requires_authentication() -> None:
    assert client.get("/v1/ai/status").status_code == 401


def test_status_is_degraded_when_no_ai_is_configured(owner_user) -> None:
    response = _get_status(owner_user, _gateway("extractive_fallback"), None)

    assert response.status_code == 200
    assert response.json() == {
        "mode": "degraded",
        "source": "none",
        "provider": "none",
        "model": None,
    }


def test_status_reports_the_organizations_own_provider(owner_user) -> None:
    response = _get_status(owner_user, _gateway("extractive_fallback"), _org_provider())

    assert response.json() == {
        "mode": "ai",
        "source": "organization",
        "provider": "openrouter",
        "model": "z-ai/glm-test",
    }


def test_status_reports_an_instance_level_provider(owner_user) -> None:
    response = _get_status(owner_user, _gateway("openai_compatible", "z-ai/glm-inst"), None)

    assert response.json()["mode"] == "ai"
    assert response.json()["source"] == "instance"
    assert response.json()["model"] == "z-ai/glm-inst"


def test_status_never_contains_key_material(owner_user) -> None:
    response = _get_status(owner_user, _gateway("openai_compatible", "m"), _org_provider())

    body = response.text.lower()
    assert "key" not in body
    assert "secret" not in body
    assert "bearer" not in body


def test_status_reporting_makes_no_provider_call() -> None:
    gateway = _gateway("openai_compatible")

    with patch(
        "app.application.ai_status_service.resolve_org_completion_provider", return_value=None
    ):
        get_ai_status(MagicMock(), MagicMock(), gateway)

    gateway.complete.assert_not_called()
    gateway.embed.assert_not_called()
