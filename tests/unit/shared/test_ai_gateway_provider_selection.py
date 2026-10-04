"""Regression coverage for `get_ai_gateway()`'s provider-selection logic
(Phase 2) — the one change in this phase capable of silently altering
already-shipped RAG chat behavior everywhere if gotten wrong, so it's
covered independently of any Phase 2 feature actually being exercised."""

import pytest
from vault_shared.ai_gateway import get_ai_gateway
from vault_shared.ai_gateway.providers.extractive_completion_provider import (
    ExtractiveCompletionProvider,
)
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)
from vault_shared.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    get_ai_gateway.cache_clear()
    get_settings.cache_clear()
    yield
    get_ai_gateway.cache_clear()
    get_settings.cache_clear()


def _override_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    settings = Settings(**overrides)
    monkeypatch.setattr("vault_shared.ai_gateway.get_settings", lambda: settings)


def test_defaults_to_the_extractive_stub_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicit, not relied-on-by-omission: `Settings()` with zero overrides
    # still reads the real `.env` file (pydantic-settings' `env_file`
    # config is CWD-relative, not test-isolated) — `apps/backend/.env` is
    # a symlink to the repo-root `.env`, which now has real
    # `COMPLETION_*` values configured for live use. Passing these two
    # explicitly is what actually exercises "nothing configured," the
    # same way the other tests in this file already do for their own cases.
    _override_settings(monkeypatch, completion_provider="extractive", completion_api_key="")

    gateway = get_ai_gateway()

    assert gateway.completion_provider_name == "extractive_fallback"


def test_stays_on_the_stub_when_provider_is_set_but_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical regression guard: flipping COMPLETION_PROVIDER alone,
    without a key, must never break every existing `complete()` caller."""
    _override_settings(monkeypatch, completion_provider="openai_compatible", completion_api_key="")

    gateway = get_ai_gateway()

    assert gateway.completion_provider_name == "extractive_fallback"


def test_uses_the_real_provider_only_when_provider_key_and_model_are_all_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(
        monkeypatch,
        completion_provider="openai_compatible",
        completion_api_key="a-real-key",
        completion_model_name="z-ai/glm-test",
    )

    gateway = get_ai_gateway()

    assert gateway.completion_provider_name == "openai_compatible"
    assert gateway.completion_model_name == "z-ai/glm-test"


def test_a_configured_key_without_a_model_name_keeps_the_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model slug is never guessed — a half-configured provider must
    degrade to the deterministic stub, not fail every AI call."""
    _override_settings(
        monkeypatch,
        completion_provider="openai_compatible",
        completion_api_key="a-real-key",
        completion_model_name="",
    )

    assert get_ai_gateway().completion_provider_name == "extractive_fallback"


def test_stub_provider_class_matches_the_default_regardless_of_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, completion_provider="extractive", completion_api_key="a-key")

    gateway = get_ai_gateway()

    assert gateway.completion_provider_name == ExtractiveCompletionProvider.name
    assert gateway.completion_provider_name != OpenAICompatibleCompletionProvider.name
