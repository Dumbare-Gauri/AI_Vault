import pytest
from cryptography.fernet import Fernet

from vault_shared.settings import Settings, enforce_production_settings


def _safe_production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "jwt_secret": "x" * 48,
        "connector_encryption_key": Fernet.generate_key().decode(),
        "cookie_secure": True,
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "object_storage_access_key": "prod-access",
        "object_storage_secret_key": "prod-secret",
        "cors_allow_origins": "https://vault.example.com",
        "frontend_url": "https://vault.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_a_fully_configured_production_environment_has_no_problems() -> None:
    assert _safe_production_settings().production_problems() == []


@pytest.mark.parametrize(
    ("override", "expected_fragment"),
    [
        ({"jwt_secret": "changeme-in-env-use-a-real-32-byte-secret"}, "JWT_SECRET"),
        # The `.env.example` placeholder — long enough and not the code default.
        ({"jwt_secret": "changeme-use-a-real-random-32-byte-secret"}, "JWT_SECRET"),
        ({"jwt_secret": "ChangeMe-" + "x" * 40}, "JWT_SECRET"),
        ({"jwt_secret": "short"}, "JWT_SECRET"),
        ({"connector_encryption_key": ""}, "CONNECTOR_ENCRYPTION_KEY"),
        ({"connector_encryption_key": "not-a-fernet-key"}, "CONNECTOR_ENCRYPTION_KEY"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"google_client_id": ""}, "GOOGLE_CLIENT_ID"),
        ({"google_client_secret": ""}, "GOOGLE_CLIENT_ID"),
        (
            {
                "object_storage_access_key": "vault-minio",
                "object_storage_secret_key": "vault-minio-secret",
            },
            "OBJECT_STORAGE",
        ),
        ({"cors_allow_origins": "https://ok.example,http://localhost:5173"}, "CORS_ALLOW_ORIGINS"),
        ({"frontend_url": "http://vault.example.com"}, "FRONTEND_URL"),
    ],
)
def test_each_unsafe_setting_is_reported_by_name(
    override: dict[str, object], expected_fragment: str
) -> None:
    problems = _safe_production_settings(**override).production_problems()

    assert any(expected_fragment in problem for problem in problems)


def test_startup_is_refused_in_production_with_development_defaults() -> None:
    settings = Settings(_env_file=None, environment="production")

    with pytest.raises(RuntimeError, match="Refusing to start in production"):
        enforce_production_settings(settings)


def test_the_refusal_names_settings_but_never_their_values() -> None:
    secret = "super-secret-jwt-value-that-is-long-enough-1234567890"
    settings = _safe_production_settings(jwt_secret=secret, cookie_secure=False)

    with pytest.raises(RuntimeError) as caught:
        enforce_production_settings(settings)

    assert secret not in str(caught.value)
    assert "COOKIE_SECURE" in str(caught.value)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_development_and_test_environments_are_never_blocked(environment: str) -> None:
    enforce_production_settings(Settings(_env_file=None, environment=environment))


def test_a_safe_production_configuration_starts() -> None:
    enforce_production_settings(_safe_production_settings())


def test_the_ai_temperature_and_model_are_configurable_not_hardcoded() -> None:
    settings = Settings(
        _env_file=None,
        completion_model_name="z-ai/glm-test",
        ai_temperature=0.4,
        ai_gateway_max_attempts=5,
    )

    assert settings.completion_model_name == "z-ai/glm-test"
    assert settings.ai_temperature == 0.4
    assert settings.ai_gateway_max_attempts == 5


def test_no_completion_credentials_or_model_are_baked_into_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.completion_api_key == ""
    assert settings.completion_model_name == ""
