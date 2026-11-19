from typing import Any

import requests

from vault_shared import AIUnavailableError, get_logger, get_request_id, get_task_id
from vault_shared.ai_gateway.boundary import attach_untrusted_context
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers.error_detail import rejected_message

logger = get_logger("vault_shared.ai_gateway.openai_compatible_completion_provider")

_QUOTA_STATUS = 402


class OpenAICompatibleCompletionProvider:
    """A hosted LLM behind `AIGateway.complete()` — in production, GLM
    through OpenRouter, though any endpoint speaking the OpenAI
    chat-completions shape works via `base_url`/`model_name` alone.

    This adapter is *reasoning transport only*. The request it builds never
    contains `tools`, `functions` or `tool_choice`, so the model has nothing
    to call; retrieved `context` is delivered as untrusted user-role data,
    never as a system message; and every failure — auth, rate limit,
    timeout, malformed body — is normalized to `AIUnavailableError` so
    callers degrade instead of crashing (and a rejected server API key can
    never be mistaken for an expired user session)."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
        temperature: float = 0.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model_name = model_name
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._extra_headers = dict(extra_headers or {})

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        outgoing = attach_untrusted_context(messages, context) if context else list(messages)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        correlation_id = get_request_id() or get_task_id()
        if correlation_id:
            headers["X-Request-Id"] = correlation_id

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in outgoing],
            "max_tokens": max_tokens,
            "temperature": self._temperature,
        }

        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise AIUnavailableError(
                "The AI provider timed out.", reason=AIUnavailableError.TIMEOUT
            ) from exc
        except requests.RequestException as exc:
            raise AIUnavailableError(
                "Could not reach the AI provider.", reason=AIUnavailableError.UNREACHABLE
            ) from exc

        self._raise_for_status(response)
        return self._parse(response)

    def _raise_for_status(self, response: requests.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        logger.warning("completion_provider_request_failed", extra={"status_code": status})
        if status == 401:
            raise AIUnavailableError(
                "The AI provider rejected the API key.", reason=AIUnavailableError.AUTH_FAILED
            )
        if status == _QUOTA_STATUS:
            raise AIUnavailableError(
                "The AI provider account is out of credits.",
                reason=AIUnavailableError.QUOTA_EXCEEDED,
            )
        if status == 429:
            raise AIUnavailableError(
                "The AI provider rate limit was exceeded.",
                reason=AIUnavailableError.RATE_LIMITED,
                retry_after_seconds=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if status >= 500:
            raise AIUnavailableError(
                f"The AI provider is unavailable ({status}).",
                reason=AIUnavailableError.PROVIDER_ERROR,
            )
        raise AIUnavailableError(
            rejected_message(status, response),
            reason=AIUnavailableError.REJECTED_REQUEST,
        )

    def _parse(self, response: requests.Response) -> CompletionResult:
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise _invalid("The AI provider returned a non-JSON response.") from exc
        if not isinstance(body, dict):
            raise _invalid("The AI provider returned an unexpected response shape.")

        # OpenRouter can answer HTTP 200 with an `error` object (e.g. an
        # upstream model failure) instead of `choices`.
        if "error" in body and "choices" not in body:
            raise AIUnavailableError(
                "The AI provider reported an upstream error.",
                reason=AIUnavailableError.PROVIDER_ERROR,
            )

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _invalid("The AI provider response had no message content.") from exc
        if not isinstance(text, str) or not text.strip():
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length" or choice["message"].get("reasoning"):
                raise _invalid(
                    "The model spent its whole reply on reasoning and gave no answer. "
                    "Choose a non-reasoning model, or one with a larger reply budget."
                )
            raise _invalid("The AI provider returned an empty message.")

        usage = body.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return CompletionResult(
            text=text,
            provider=self.name,
            model_name=str(body.get("model") or self.model_name),
            tokens_used=tokens if isinstance(tokens, int) else None,
        )


def _invalid(message: str) -> AIUnavailableError:
    return AIUnavailableError(message, reason=AIUnavailableError.INVALID_RESPONSE)


def _parse_retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None
