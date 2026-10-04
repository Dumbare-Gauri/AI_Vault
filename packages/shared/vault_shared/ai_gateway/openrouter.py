"""The one place completion providers are constructed.

Organization-supplied API keys are only ever sent to OpenRouter's fixed base
URL (`OPENROUTER_BASE_URL`), never to a URL an organization can choose — a
tenant must not be able to point the server at an arbitrary endpoint.
"""

from vault_shared import AIUnavailableError, get_settings
from vault_shared.ai_gateway.gateway import AIGateway, GatewayPolicy
from vault_shared.ai_gateway.interfaces import EmbeddingResult, Message
from vault_shared.ai_gateway.providers.openai_compatible_completion_provider import (
    OpenAICompatibleCompletionProvider,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_CONNECTION_TEST_MESSAGE = "Reply with exactly one word: OK."
_CONNECTION_TEST_MAX_TOKENS = 10

_REASON_MESSAGES = {
    AIUnavailableError.AUTH_FAILED: "That API key was rejected.",
    AIUnavailableError.QUOTA_EXCEEDED: "The account is out of credits.",
    AIUnavailableError.RATE_LIMITED: "The provider is rate limiting requests — try again shortly.",
    AIUnavailableError.TIMEOUT: "The provider timed out.",
    AIUnavailableError.UNREACHABLE: "Could not reach the provider.",
}


def build_provider(
    *, api_key: str, model_name: str, base_url: str = OPENROUTER_BASE_URL
) -> OpenAICompatibleCompletionProvider:
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.completion_http_referer:
        headers["HTTP-Referer"] = settings.completion_http_referer
    if settings.completion_app_title:
        headers["X-Title"] = settings.completion_app_title
    return OpenAICompatibleCompletionProvider(
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=settings.completion_request_timeout_seconds,
        temperature=settings.ai_temperature,
        extra_headers=headers,
    )


def build_gateway_policy() -> GatewayPolicy:
    settings = get_settings()
    return GatewayPolicy(
        max_attempts=settings.ai_gateway_max_attempts,
        base_delay_seconds=settings.ai_gateway_retry_base_delay_seconds,
        max_delay_seconds=settings.ai_gateway_retry_max_delay_seconds,
        max_requests_per_minute=settings.ai_gateway_max_requests_per_minute,
    )


class _NoEmbeddings:
    name = "none"
    model_name = "none"
    model_version = "0"

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        raise AIUnavailableError(
            "Embeddings are not available on this gateway.",
            reason=AIUnavailableError.NOT_CONFIGURED,
        )


def check_connection(*, api_key: str, model_name: str) -> tuple[bool, str | None]:
    """Validates a candidate key/model through the gateway (so timeouts,
    error normalization and logging apply) without persisting anything.
    One attempt only — a user is waiting on the answer."""
    gateway = AIGateway(
        embedding_provider=_NoEmbeddings(),
        completion_provider=build_provider(api_key=api_key, model_name=model_name),
        policy=GatewayPolicy(max_attempts=1),
    )
    try:
        gateway.complete(
            messages=[Message(role="user", content=_CONNECTION_TEST_MESSAGE)],
            max_tokens=_CONNECTION_TEST_MAX_TOKENS,
        )
    except AIUnavailableError as exc:
        return False, _REASON_MESSAGES.get(exc.reason, exc.message)
    return True, None
