from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import (
    get_ai_provider_config_service,
    get_organization_service,
)

client = TestClient(app)


def test_get_current_organization_requires_authentication() -> None:
    response = client.get("/v1/organizations/current")
    assert response.status_code == 401


def test_get_current_organization_returns_the_users_organization(owner_user) -> None:
    app.dependency_overrides[get_current_user] = lambda: owner_user
    try:
        response = client.get("/v1/organizations/current")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["slug"] == owner_user.organization.slug


def test_owner_can_rename_the_organization(owner_user) -> None:
    fake_service = MagicMock()
    renamed = owner_user.organization
    renamed.name = "New Name"
    fake_service.rename.return_value = renamed

    app.dependency_overrides[get_current_user] = lambda: owner_user
    app.dependency_overrides[get_organization_service] = lambda: fake_service
    try:
        response = client.patch("/v1/organizations/current", json={"name": "New Name"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_organization_service, None)

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"
    fake_service.rename.assert_called_once_with(owner_user.organization, name="New Name")


def test_member_cannot_rename_the_organization(member_user) -> None:
    fake_service = MagicMock()

    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_organization_service] = lambda: fake_service
    try:
        response = client.patch("/v1/organizations/current", json={"name": "New Name"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_organization_service, None)

    assert response.status_code == 403
    fake_service.rename.assert_not_called()


def test_rename_requires_a_non_empty_name(owner_user) -> None:
    app.dependency_overrides[get_current_user] = lambda: owner_user
    try:
        response = client.patch("/v1/organizations/current", json={"name": ""})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 422


def test_get_ai_provider_config_requires_authentication() -> None:
    response = client.get("/v1/organizations/current/ai-provider")
    assert response.status_code == 401


def test_get_ai_provider_config_reports_unconfigured_by_default(owner_user) -> None:
    fake_service = MagicMock()
    fake_service.get_status.return_value = None

    app.dependency_overrides[get_current_user] = lambda: owner_user
    app.dependency_overrides[get_ai_provider_config_service] = lambda: fake_service
    try:
        response = client.get("/v1/organizations/current/ai-provider")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_ai_provider_config_service, None)

    assert response.status_code == 200
    body = response.json()
    assert (body["configured"], body["model_name"], body["provider"]) == (False, None, None)
    assert {option["id"] for option in body["available_providers"]} == {
        "openrouter",
        "openai",
        "anthropic",
        "gemini",
        "ollama",
        "zai",
        "deepseek",
        "groq",
        "mistral",
        "xai",
    }


def test_a_member_cannot_set_the_ai_provider_config(member_user) -> None:
    fake_service = MagicMock()

    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_ai_provider_config_service] = lambda: fake_service
    try:
        response = client.put(
            "/v1/organizations/current/ai-provider",
            json={"api_key": "sk-or-v1-test", "model_name": "z-ai/glm-5.2:free"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_ai_provider_config_service, None)

    assert response.status_code == 403
    fake_service.set.assert_not_called()


def test_a_member_cannot_clear_the_ai_provider_config(member_user) -> None:
    fake_service = MagicMock()

    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_ai_provider_config_service] = lambda: fake_service
    try:
        response = client.delete("/v1/organizations/current/ai-provider")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_ai_provider_config_service, None)

    assert response.status_code == 403
    fake_service.clear.assert_not_called()


def test_owner_can_set_the_ai_provider_config(owner_user) -> None:
    from vault_shared.db.models import AIProviderConfig

    fake_service = MagicMock()
    config = AIProviderConfig(
        organization_id=owner_user.organization_id,
        model_name="z-ai/glm-5.2:free",
        provider="anthropic",
    )
    fake_service.set.return_value = config

    app.dependency_overrides[get_current_user] = lambda: owner_user
    app.dependency_overrides[get_ai_provider_config_service] = lambda: fake_service
    try:
        response = client.put(
            "/v1/organizations/current/ai-provider",
            json={
                "api_key": "sk-or-v1-test",
                "model_name": "z-ai/glm-5.2:free",
                "provider": "anthropic",
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_ai_provider_config_service, None)

    assert response.status_code == 200
    assert (response.json()["configured"], response.json()["provider"]) == (True, "anthropic")
    fake_service.set.assert_called_once_with(
        owner_user.organization_id,
        api_key="sk-or-v1-test",
        model_name="z-ai/glm-5.2:free",
        provider="anthropic",
    )


def test_a_member_cannot_test_the_ai_provider_config(member_user) -> None:
    fake_service = MagicMock()

    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_ai_provider_config_service] = lambda: fake_service
    try:
        response = client.post(
            "/v1/organizations/current/ai-provider/test",
            json={"api_key": "sk-or-v1-test", "model_name": "z-ai/glm-5.2:free"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_ai_provider_config_service, None)

    assert response.status_code == 403
    fake_service.test.assert_not_called()
