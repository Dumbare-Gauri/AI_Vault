from unittest.mock import MagicMock, patch

import pytest
import requests
from vault_shared import AIUnavailableError, DependencyUnavailableError, UnauthorizedError
from vault_shared.ai_gateway.interfaces import Message
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)
from vault_shared.logging import set_request_id


def _response(status_code: int, json_body: object = None, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    response.headers = headers or {}
    return response


def _provider(**kwargs) -> OpenAICompatibleCompletionProvider:
    return OpenAICompatibleCompletionProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model_name="z-ai/glm-test",
        timeout_seconds=60,
        **kwargs,
    )


def _complete(provider: OpenAICompatibleCompletionProvider | None = None):
    return (provider or _provider()).complete(
        messages=[Message(role="user", content="hi")], context=None, max_tokens=100
    )


def _raises_ai_error(response=None, side_effect=None):
    return patch("requests.post", return_value=response, side_effect=side_effect)


def test_maps_a_successful_response_to_a_completion_result() -> None:
    body = {
        "choices": [{"message": {"content": '{"summary": "A contract."}'}}],
        "model": "z-ai/glm-test",
        "usage": {"total_tokens": 123},
    }
    with patch("requests.post", return_value=_response(200, body)) as mock_post:
        result = _provider().complete(
            messages=[Message(role="user", content="Summarize this.")],
            context=None,
            max_tokens=500,
        )

    assert result.text == '{"summary": "A contract."}'
    assert result.provider == "openai_compatible"
    assert result.model_name == "z-ai/glm-test"
    assert result.tokens_used == 123

    call_kwargs = mock_post.call_args.kwargs
    assert mock_post.call_args.args[0] == "https://openrouter.ai/api/v1/chat/completions"
    assert call_kwargs["json"]["model"] == "z-ai/glm-test"
    assert call_kwargs["json"]["messages"] == [{"role": "user", "content": "Summarize this."}]
    assert call_kwargs["json"]["max_tokens"] == 500
    assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"


def test_the_request_never_offers_the_model_any_tools() -> None:
    body = {"choices": [{"message": {"content": "ok"}}]}
    with patch("requests.post", return_value=_response(200, body)) as mock_post:
        _complete()

    payload = mock_post.call_args.kwargs["json"]
    assert not {"tools", "functions", "tool_choice", "function_call"} & set(payload)


def test_temperature_and_attribution_headers_are_configurable() -> None:
    body = {"choices": [{"message": {"content": "ok"}}]}
    provider = _provider(temperature=0.3, extra_headers={"X-Title": "AI Vault"})
    with patch("requests.post", return_value=_response(200, body)) as mock_post:
        _complete(provider)

    assert mock_post.call_args.kwargs["json"]["temperature"] == 0.3
    assert mock_post.call_args.kwargs["headers"]["X-Title"] == "AI Vault"


def test_the_correlation_id_is_forwarded_when_one_is_set() -> None:
    body = {"choices": [{"message": {"content": "ok"}}]}
    set_request_id("req-abc-123")
    try:
        with patch("requests.post", return_value=_response(200, body)) as mock_post:
            _complete()
    finally:
        set_request_id(None)

    assert mock_post.call_args.kwargs["headers"]["X-Request-Id"] == "req-abc-123"


def test_context_is_delivered_as_untrusted_user_data_never_a_system_message() -> None:
    body = {"choices": [{"message": {"content": "ok"}}]}
    with patch("requests.post", return_value=_response(200, body)) as mock_post:
        _provider().complete(
            messages=[Message(role="user", content="What is this?")],
            context="### Contract.pdf\nParty A and Party B agree...",
            max_tokens=100,
        )

    messages = mock_post.call_args.kwargs["json"]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert "<untrusted_data" in messages[0]["content"]
    assert "Party A and Party B agree" in messages[0]["content"]


def test_401_is_an_ai_failure_not_an_unauthorized_user() -> None:
    """A rejected server-side AI key must never surface as HTTP 401, which
    would make the frontend treat the *user's* session as expired."""
    with patch("requests.post", return_value=_response(401)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.AUTH_FAILED
    assert not isinstance(caught.value, UnauthorizedError)
    assert caught.value.http_status == 503
    assert caught.value.retryable is False


def test_402_is_a_non_retryable_quota_failure() -> None:
    with patch("requests.post", return_value=_response(402)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.QUOTA_EXCEEDED
    assert caught.value.retryable is False


def test_429_is_retryable_and_carries_the_provider_retry_after() -> None:
    with patch("requests.post", return_value=_response(429, headers={"Retry-After": "7"})):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.RATE_LIMITED
    assert caught.value.retry_after_seconds == 7.0
    assert caught.value.retryable is True
    assert isinstance(caught.value, DependencyUnavailableError)


def test_a_5xx_is_a_retryable_provider_error() -> None:
    with patch("requests.post", return_value=_response(503)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.PROVIDER_ERROR
    assert caught.value.retryable is True


def test_a_4xx_rejection_is_not_retried() -> None:
    with patch("requests.post", return_value=_response(400)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.retryable is False


def test_a_timeout_is_reported_as_a_timeout() -> None:
    with patch("requests.post", side_effect=requests.Timeout("slow")):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.TIMEOUT
    assert caught.value.retryable is True


def test_a_connection_error_is_reported_as_unreachable() -> None:
    with patch("requests.post", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.UNREACHABLE


@pytest.mark.parametrize(
    "body",
    [
        "not-a-dict",
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "   "}}]},
    ],
)
def test_a_malformed_body_is_an_invalid_response_not_a_crash(body: object) -> None:
    with patch("requests.post", return_value=_response(200, body)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.INVALID_RESPONSE
    assert caught.value.retryable is False


def test_a_non_json_body_is_an_invalid_response() -> None:
    response = _response(200)
    response.json.side_effect = ValueError("no json")
    with patch("requests.post", return_value=response):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.INVALID_RESPONSE


def test_an_upstream_error_object_on_http_200_is_a_provider_error() -> None:
    body = {"error": {"code": 502, "message": "upstream failed"}}
    with patch("requests.post", return_value=_response(200, body)):
        with pytest.raises(AIUnavailableError) as caught:
            _complete()

    assert caught.value.reason == AIUnavailableError.PROVIDER_ERROR


def test_falls_back_to_configured_model_name_when_response_omits_it() -> None:
    body = {"choices": [{"message": {"content": "ok"}}]}  # no "model" key
    with patch("requests.post", return_value=_response(200, body)):
        result = _complete()

    assert result.model_name == "z-ai/glm-test"
    assert result.tokens_used is None


def test_a_rejected_request_says_why_in_the_providers_own_words() -> None:
    body = {
        "error": {"message": "nvidia/nemotron-3-ultra:free is not a valid model ID", "code": 400}
    }

    with _raises_ai_error(_response(400, body)), pytest.raises(AIUnavailableError) as error:
        _complete()

    assert "not a valid model ID" in str(error.value)
    assert error.value.reason == AIUnavailableError.REJECTED_REQUEST


def test_a_very_long_provider_reason_is_shortened() -> None:
    body = {"error": {"message": "x" * 5000}}

    with _raises_ai_error(_response(400, body)), pytest.raises(AIUnavailableError) as error:
        _complete()

    assert len(str(error.value)) < 300


def test_a_model_that_only_reasoned_is_explained() -> None:
    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "", "reasoning": "Let me think about how to reply..."},
            }
        ]
    }

    with _raises_ai_error(_response(200, body)), pytest.raises(AIUnavailableError) as error:
        _complete()

    assert "reasoning" in str(error.value)
