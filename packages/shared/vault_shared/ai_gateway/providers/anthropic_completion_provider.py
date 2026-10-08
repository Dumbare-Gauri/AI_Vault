from typing import Any

import requests

from vault_shared import AIUnavailableError, get_logger
from vault_shared.ai_gateway.boundary import attach_untrusted_context
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers.error_detail import rejected_message

logger = get_logger("vault_shared.ai_gateway.anthropic_completion_provider")

_API_VERSION = "2023-06-01"
_OVERLOADED_STATUS = 529


class AnthropicCompletionProvider:
    """Anthropic's native Messages API behind `AIGateway.complete()`. Same
    contract as the OpenAI-compatible adapter: reasoning transport only — no
    tools are ever sent — system instructions travel in Anthropic's
    top-level `system` field, retrieved context stays untrusted user-role
    data, and every failure is normalized to `AIUnavailableError`."""

    name = "anthropic"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: int,
        temperature: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model_name = model_name
        self._timeout = timeout_seconds
        self._temperature = temperature

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        outgoing = attach_untrusted_context(messages, context) if context else list(messages)
        system = "\n\n".join(m.content for m in outgoing if m.role == "system")
        conversation = [
            {"role": m.role, "content": m.content}
            for m in outgoing
            if m.role in ("user", "assistant")
        ]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "temperature": self._temperature,
            "messages": conversation,
        }
        if system:
            payload["system"] = system

        try:
            response = requests.post(
                f"{self._base_url}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _API_VERSION,
                    "content-type": "application/json",
                },
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
        try:
            body: Any = response.json()
            text = "".join(
                block.get("text", "")
                for block in body["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise AIUnavailableError(
                "The AI provider returned an unexpected response.",
                reason=AIUnavailableError.INVALID_RESPONSE,
            ) from exc
        if not text.strip():
            raise AIUnavailableError(
                "The AI provider returned an empty message.",
                reason=AIUnavailableError.INVALID_RESPONSE,
            )
        usage = body.get("usage") if isinstance(body, dict) else None
        tokens = (
            (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            if isinstance(usage, dict)
            else None
        )
        return CompletionResult(
            text=text, provider=self.name, model_name=self.model_name, tokens_used=tokens
        )

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        logger.warning("completion_provider_request_failed", extra={"status_code": status})
        if status in (401, 403):
            raise AIUnavailableError(
                "The AI provider rejected the API key.", reason=AIUnavailableError.AUTH_FAILED
            )
        if status == 429:
            raise AIUnavailableError(
                "The AI provider rate limit was exceeded.",
                reason=AIUnavailableError.RATE_LIMITED,
            )
        if status >= 500 or status == _OVERLOADED_STATUS:
            raise AIUnavailableError(
                f"The AI provider is unavailable ({status}).",
                reason=AIUnavailableError.PROVIDER_ERROR,
            )
        raise AIUnavailableError(
            rejected_message(status, response),
            reason=AIUnavailableError.REJECTED_REQUEST,
        )
