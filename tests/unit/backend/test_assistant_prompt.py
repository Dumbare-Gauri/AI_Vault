from app.application.assistant.prompts import (
    STORAGE_ASSISTANT_SYSTEM_PROMPT,
    STORAGE_ASSISTANT_SYSTEM_PROMPT_VERSION,
)


def test_prompt_version_is_set() -> None:
    assert STORAGE_ASSISTANT_SYSTEM_PROMPT_VERSION


def test_prompt_establishes_identity() -> None:
    assert "Ask Vault" in STORAGE_ASSISTANT_SYSTEM_PROMPT


def test_prompt_forbids_fabrication() -> None:
    assert "never" in STORAGE_ASSISTANT_SYSTEM_PROMPT.lower()
    assert "<untrusted_data>" in STORAGE_ASSISTANT_SYSTEM_PROMPT


def test_prompt_forbids_deletion_recommendations() -> None:
    assert "safe to delete" in STORAGE_ASSISTANT_SYSTEM_PROMPT.lower()


def test_prompt_never_lets_the_model_claim_a_change() -> None:
    lowered = STORAGE_ASSISTANT_SYSTEM_PROMPT.lower()
    assert "never change storage yourself" in lowered
    assert "never claim something was changed" in lowered


def test_prompt_establishes_prompt_injection_defense() -> None:
    lowered = STORAGE_ASSISTANT_SYSTEM_PROMPT.lower()
    assert "instructions" in lowered
    assert "data" in lowered


def test_prompt_requires_freshness_disclosure() -> None:
    assert "stale" in STORAGE_ASSISTANT_SYSTEM_PROMPT.lower()
