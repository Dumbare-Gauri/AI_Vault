import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from vault_shared import AIUnavailableError, VaultError, get_logger
from vault_shared.ai_gateway.boundary import AIContext
from vault_shared.ai_gateway.interfaces import (
    CompletionProvider,
    CompletionResult,
    EmbeddingProvider,
    EmbeddingResult,
    Message,
)
from vault_shared.ai_gateway.recommendation import (
    RECOMMENDATION_OUTPUT_INSTRUCTIONS,
    AIResponseRejected,
    ValidatedRecommendation,
    validate_recommendation,
)
from vault_shared.metrics import record_ai_failure, record_ai_provider_latency, record_ai_tokens

logger = get_logger("vault_shared.ai_gateway")


@dataclass(frozen=True)
class GatewayPolicy:
    """Retry and rate-limit behavior for completion calls. The defaults are
    'no retries, no limiter' so directly-constructed gateways (tests, one-off
    checks) behave exactly as before; `get_ai_gateway()` supplies the
    production values from settings."""

    max_attempts: int = 1
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 10.0
    max_requests_per_minute: int = 0
    max_limiter_wait_seconds: float = 5.0


class SlidingWindowLimiter:
    """Per-process request limiter protecting the provider quota (notably
    OpenRouter's free-tier limits) from a worker loop or a burst of chats.
    It bounds one process; cross-process limiting is the job of the
    route-level Redis limiter in front of user-triggered calls."""

    def __init__(
        self,
        max_requests: int,
        *,
        window_seconds: float = 60.0,
        max_wait_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._max_wait = max_wait_seconds
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self._window:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                wait = self._window - (now - self._stamps[0])
            if waited + wait > self._max_wait:
                raise AIUnavailableError(
                    "The local AI request rate limit was reached.",
                    reason=AIUnavailableError.RATE_LIMITED,
                    retry_after_seconds=wait,
                )
            self._sleep(wait)
            waited += wait


class AIGateway:
    """The sole boundary between this platform and any embedding/LLM
    provider (Handbook §8.14, §12) — every caller uses these capability
    methods only; nothing outside this module and its `providers/`
    adapters imports a provider SDK or calls a provider API directly.

    The gateway is *reasoning only*. It exposes no storage, database,
    filesystem or execution capability, sends the model no tools, and
    treats everything the model returns as an untrusted recommendation
    (see `recommend()`). Failures are normalized to `AIUnavailableError`
    so the rest of the product degrades instead of breaking.

    Provider selection is constructor injection (`get_ai_gateway()`
    decides which adapters to wire) — swapping or adding a provider is a
    change to the factory plus a new adapter class, never a change to any
    caller of `embed`/`complete`."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        completion_provider: CompletionProvider,
        policy: GatewayPolicy | None = None,
        limiter: SlidingWindowLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._completion_provider = completion_provider
        self._policy = policy or GatewayPolicy()
        self._sleep = sleep
        if limiter is None and self._policy.max_requests_per_minute > 0:
            limiter = SlidingWindowLimiter(
                self._policy.max_requests_per_minute,
                max_wait_seconds=self._policy.max_limiter_wait_seconds,
            )
        self._limiter = limiter

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        started_at = time.perf_counter()
        try:
            return self._embedding_provider.embed(texts)
        finally:
            record_ai_provider_latency(
                # `.name` (the provider identifier), not `.model_name` (the
                # specific model version) — matches CompletionProvider's
                # only identity attribute below, keeping the metric's
                # `provider` label shape consistent across both operations.
                provider=self._embedding_provider.name,
                operation="embed",
                duration_seconds=time.perf_counter() - started_at,
            )

    @property
    def embedding_model_name(self) -> str:
        """Lets a caller (e.g. `EmbeddingService`'s pending-files query)
        check whether a stored embedding is current without calling
        `embed()` — the provider exposes its identity as plain
        attributes for exactly this."""
        return self._embedding_provider.model_name

    @property
    def embedding_model_version(self) -> str:
        return self._embedding_provider.model_version

    @property
    def completion_provider_name(self) -> str:
        """Lets a caller (e.g. `IntelligenceService`) detect the stubbed
        `ExtractiveCompletionProvider` (`name == "extractive_fallback"`)
        and fail fast/cleanly instead of calling `complete()` for every
        pending file, getting non-JSON back, and marking each one FAILED
        as if it were a per-file problem."""
        return self._completion_provider.name

    @property
    def completion_model_name(self) -> str:
        """Same reasoning as `embedding_model_name` — lets a caller (e.g.
        `IntelligenceService`'s pending-files query) detect a model swap
        without calling `complete()`."""
        return self._completion_provider.model_name

    def with_completion_provider(self, completion_provider: CompletionProvider) -> "AIGateway":
        """Returns a *new* `AIGateway` reusing this instance's embedding
        provider, policy and limiter by reference — never reconstructs
        `LocalEmbeddingProvider` (its GloVe word-vector load is expensive
        and meant to happen at most once per process). Lets one request/task
        bind an organization-specific completion provider without touching
        the process-wide cached singleton `get_ai_gateway()` returns."""
        return AIGateway(
            embedding_provider=self._embedding_provider,
            completion_provider=completion_provider,
            policy=self._policy,
            limiter=self._limiter,
            sleep=self._sleep,
        )

    def complete(
        self, *, messages: list[Message], context: str | None = None, max_tokens: int = 1024
    ) -> CompletionResult:
        provider = self._completion_provider
        started_at = time.perf_counter()
        attempts = 0
        try:
            while True:
                attempts += 1
                if self._limiter is not None:
                    self._limiter.acquire()
                try:
                    result = provider.complete(
                        messages=messages, context=context, max_tokens=max_tokens
                    )
                    break
                except AIUnavailableError as exc:
                    if not exc.retryable or attempts >= self._policy.max_attempts:
                        raise
                    self._sleep(self._retry_delay(attempts, exc))
                except VaultError:
                    raise
                except Exception as exc:
                    logger.exception("ai_provider_unexpected_error")
                    raise AIUnavailableError(
                        "The AI provider failed unexpectedly.",
                        reason=AIUnavailableError.PROVIDER_ERROR,
                    ) from exc
        except AIUnavailableError as exc:
            record_ai_failure(provider=provider.name, reason=exc.reason)
            logger.warning(
                "ai_completion_failed",
                extra={"provider": provider.name, "reason": exc.reason, "attempts": attempts},
            )
            raise
        finally:
            record_ai_provider_latency(
                provider=provider.name,
                operation="complete",
                duration_seconds=time.perf_counter() - started_at,
            )

        # Sizes and counts only — never message content.
        logger.info(
            "ai_completion",
            extra={
                "provider": result.provider,
                "ai_model": result.model_name,
                "tokens_used": result.tokens_used,
                "attempts": attempts,
                "prompt_chars": sum(len(m.content) for m in messages) + len(context or ""),
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
            },
        )
        if result.tokens_used:
            record_ai_tokens(
                provider=result.provider, model=result.model_name, tokens=result.tokens_used
            )
        return result

    def recommend(
        self, context: AIContext, *, system_instructions: str, max_tokens: int = 1500
    ) -> ValidatedRecommendation:
        """Reason over an explicit `AIContext` and return a validated but
        NON-authoritative recommendation. Nothing here can execute
        anything: the caller must still run authorization, policy, and any
        required human confirmation before an action exists."""
        messages = context.to_messages(
            system_instructions=f"{system_instructions}\n\n{RECOMMENDATION_OUTPUT_INSTRUCTIONS}"
        )
        completion = self.complete(messages=messages, max_tokens=max_tokens)
        try:
            return validate_recommendation(completion.text, allowed_refs=context.refs)
        except AIResponseRejected as exc:
            raise AIUnavailableError(
                "The AI returned a response that could not be used.",
                reason=AIUnavailableError.INVALID_RESPONSE,
            ) from exc

    def _retry_delay(self, attempt: int, error: AIUnavailableError) -> float:
        if error.retry_after_seconds is not None:
            return min(error.retry_after_seconds, self._policy.max_delay_seconds)
        return min(
            self._policy.base_delay_seconds * (2 ** (attempt - 1)), self._policy.max_delay_seconds
        )
