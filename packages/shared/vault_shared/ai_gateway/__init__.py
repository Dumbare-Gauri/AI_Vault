from functools import lru_cache

from vault_shared.ai_gateway.boundary import (
    AIContext,
    AIContextFile,
    UntrustedBlock,
    build_guarded_messages,
    wrap_untrusted,
)
from vault_shared.ai_gateway.gateway import AIGateway, GatewayPolicy
from vault_shared.ai_gateway.interfaces import (
    CompletionProvider,
    CompletionResult,
    EmbeddingProvider,
    EmbeddingResult,
    Message,
)
from vault_shared.ai_gateway.openrouter import build_gateway_policy, build_provider
from vault_shared.ai_gateway.providers import (
    ExtractiveCompletionProvider,
    LocalEmbeddingProvider,
)
from vault_shared.ai_gateway.recommendation import (
    AIResponseRejected,
    RecommendationKind,
    ValidatedAction,
    ValidatedRecommendation,
    validate_recommendation,
)
from vault_shared.ai_gateway.similarity import rank_by_similarity
from vault_shared.settings import get_settings

__all__ = [
    "AIContext",
    "AIContextFile",
    "AIGateway",
    "AIResponseRejected",
    "CompletionProvider",
    "CompletionResult",
    "EmbeddingProvider",
    "EmbeddingResult",
    "GatewayPolicy",
    "Message",
    "RecommendationKind",
    "UntrustedBlock",
    "ValidatedAction",
    "ValidatedRecommendation",
    "build_guarded_messages",
    "get_ai_gateway",
    "rank_by_similarity",
    "validate_recommendation",
    "wrap_untrusted",
]


@lru_cache
def get_ai_gateway() -> AIGateway:
    """The factory every caller uses — `apps/backend` (query embedding +
    chat completion) and `apps/worker` (document embedding + file
    intelligence) both call this rather than constructing `AIGateway`
    themselves, so provider selection lives in exactly one place. Always
    wires the local/free embedding provider (see ADR-018). The completion
    provider is the OpenRouter/GLM adapter only when
    `completion_provider="openai_compatible"` AND both `completion_api_key`
    and `completion_model_name` are set — a half-configured setting must
    fall back to the deterministic stub, not fail every `complete()` call
    across the app (RAG chat included)."""
    settings = get_settings()
    completion_provider: CompletionProvider
    if (
        settings.completion_provider == "openai_compatible"
        and settings.completion_api_key
        and settings.completion_model_name
    ):
        completion_provider = build_provider(
            api_key=settings.completion_api_key,
            model_name=settings.completion_model_name,
            base_url=settings.completion_api_base_url,
        )
    else:
        completion_provider = ExtractiveCompletionProvider()
    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=completion_provider,
        policy=build_gateway_policy(),
    )
