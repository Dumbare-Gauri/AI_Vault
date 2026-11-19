from unittest.mock import MagicMock, patch

import pytest
import requests

from vault_shared import AIUnavailableError
from vault_shared.ai_gateway.interfaces import Message
from vault_shared.ai_gateway.openrouter import build_catalog_provider
from vault_shared.ai_gateway.providers.anthropic_completion_provider import (
    AnthropicCompletionProvider,
)
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)


def _response(status_code: int, json_body: object = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    return response


def _provider() -> AnthropicCompletionProvider:
    return AnthropicCompletionProvider(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model_name="claude-test",
        timeout_seconds=60,
    )


def _complete(messages: list[Message] | None = None, context: str | None = None):
    return _provider().complete(
        messages=messages or [Message(role="user", content="hi")],
        context=context,
        max_tokens=100,
    )


_OK_BODY = {
    "content": [{"type": "text", "text": "OK"}],
    "usage": {"input_tokens": 7, "output_tokens": 3},
}


def test_maps_a_messages_response_to_a_completion_result() -> None:
    with patch("requests.post", return_value=_response(200, _OK_BODY)):
        result = _complete()

    assert (result.text, result.provider, result.tokens_used) == ("OK", "anthropic", 10)


def test_posts_to_the_messages_endpoint_with_the_key_header() -> None:
    with patch("requests.post", return_value=_response(200, _OK_BODY)) as mock_post:
        _complete()

    assert mock_post.call_args.args[0] == "https://api.anthropic.com/v1/messages"
    assert mock_post.call_args.kwargs["headers"]["x-api-key"] == "test-key"


def test_system_messages_travel_in_the_top_level_system_field() -> None:
    with patch("requests.post", return_value=_response(200, _OK_BODY)) as mock_post:
        _complete([Message(role="system", content="Be terse."), Message(role="user", content="hi")])

    payload = mock_post.call_args.kwargs["json"]
    assert payload["system"] == "Be terse."
    assert [m["role"] for m in payload["messages"]] == ["user"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, AIUnavailableError.AUTH_FAILED),
        (429, AIUnavailableError.RATE_LIMITED),
        (529, AIUnavailableError.PROVIDER_ERROR),
        (400, AIUnavailableError.REJECTED_REQUEST),
    ],
)
def test_normalizes_http_failures(status: int, reason: str) -> None:
    with patch("requests.post", return_value=_response(status)):
        with pytest.raises(AIUnavailableError) as exc_info:
            _complete()

    assert exc_info.value.reason == reason


def test_normalizes_a_timeout() -> None:
    with patch("requests.post", side_effect=requests.Timeout()):
        with pytest.raises(AIUnavailableError) as exc_info:
            _complete()

    assert exc_info.value.reason == AIUnavailableError.TIMEOUT


def test_an_empty_text_response_is_invalid() -> None:
    with patch("requests.post", return_value=_response(200, {"content": []})):
        with pytest.raises(AIUnavailableError) as exc_info:
            _complete()

    assert exc_info.value.reason == AIUnavailableError.INVALID_RESPONSE


def test_catalog_builds_the_anthropic_adapter_for_anthropic() -> None:
    provider = build_catalog_provider(provider="anthropic", api_key="k", model_name="m")

    assert isinstance(provider, AnthropicCompletionProvider)


@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [
        ("openai", "https://api.openai.com/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("openrouter", "https://openrouter.ai/api/v1"),
    ],
)
def test_catalog_sends_keys_only_to_the_providers_fixed_endpoint(
    provider_id: str, base_url: str
) -> None:
    provider = build_catalog_provider(provider=provider_id, api_key="k", model_name="m")

    assert isinstance(provider, OpenAICompatibleCompletionProvider)
    assert provider._base_url == base_url


def test_an_unknown_provider_falls_back_to_the_default_endpoint() -> None:
    provider = build_catalog_provider(provider="evil.example", api_key="k", model_name="m")

    assert provider._base_url == "https://openrouter.ai/api/v1"
