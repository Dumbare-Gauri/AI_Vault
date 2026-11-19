import json
import logging

import pytest

from vault_shared import AIUnavailableError, NotFoundError
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.boundary import AIContext, AIContextFile
from vault_shared.ai_gateway.gateway import GatewayPolicy, SlidingWindowLimiter
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider, LocalEmbeddingProvider

_SECRET_FILE_TEXT = "CONFIDENTIAL-SALARY-TABLE-4711"


class _ScriptedProvider:
    """Plays back a list of outcomes: an Exception is raised, a str is
    returned as the completion text."""

    name = "scripted"
    model_name = "scripted-model"

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.received: list[dict] = []

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        self.calls += 1
        self.received.append({"messages": messages, "context": context, "max_tokens": max_tokens})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return CompletionResult(
            text=str(outcome), provider=self.name, model_name=self.model_name, tokens_used=10
        )


def _gateway(provider: object, *, attempts: int = 3, **policy: object) -> tuple[AIGateway, list]:
    sleeps: list[float] = []
    gateway = AIGateway(
        embedding_provider=LocalEmbeddingProvider.__new__(LocalEmbeddingProvider),
        completion_provider=provider,  # type: ignore[arg-type]
        policy=GatewayPolicy(max_attempts=attempts, **policy),  # type: ignore[arg-type]
        sleep=sleeps.append,
    )
    return gateway, sleeps


def _ask(gateway: AIGateway) -> CompletionResult:
    return gateway.complete(messages=[Message(role="user", content="hi")])


def _transient(reason: str = AIUnavailableError.PROVIDER_ERROR, **kwargs: object):
    return AIUnavailableError("boom", reason=reason, **kwargs)  # type: ignore[arg-type]


class TestRetry:
    def test_a_transient_failure_is_retried_until_it_succeeds(self) -> None:
        provider = _ScriptedProvider(_transient(), _transient(), "ok")
        gateway, _ = _gateway(provider)

        assert _ask(gateway).text == "ok"
        assert provider.calls == 3

    def test_retries_are_bounded_by_max_attempts(self) -> None:
        provider = _ScriptedProvider(*[_transient() for _ in range(10)])
        gateway, _ = _gateway(provider, attempts=3)

        with pytest.raises(AIUnavailableError):
            _ask(gateway)

        assert provider.calls == 3

    @pytest.mark.parametrize(
        "reason",
        [
            AIUnavailableError.AUTH_FAILED,
            AIUnavailableError.QUOTA_EXCEEDED,
            AIUnavailableError.INVALID_RESPONSE,
            AIUnavailableError.REJECTED_REQUEST,
            AIUnavailableError.NOT_CONFIGURED,
        ],
    )
    def test_non_retryable_failures_are_attempted_exactly_once(self, reason: str) -> None:
        provider = _ScriptedProvider(_transient(reason), "never reached")
        gateway, sleeps = _gateway(provider)

        with pytest.raises(AIUnavailableError) as caught:
            _ask(gateway)

        assert caught.value.reason == reason
        assert provider.calls == 1
        assert sleeps == []

    def test_backoff_doubles_and_is_capped(self) -> None:
        provider = _ScriptedProvider(*[_transient() for _ in range(5)])
        gateway, sleeps = _gateway(
            provider, attempts=5, base_delay_seconds=1.0, max_delay_seconds=3.0
        )

        with pytest.raises(AIUnavailableError):
            _ask(gateway)

        assert sleeps == [1.0, 2.0, 3.0, 3.0]

    def test_provider_retry_after_is_honoured_but_capped(self) -> None:
        provider = _ScriptedProvider(
            _transient(AIUnavailableError.RATE_LIMITED, retry_after_seconds=4),
            _transient(AIUnavailableError.RATE_LIMITED, retry_after_seconds=900),
            "ok",
        )
        gateway, sleeps = _gateway(provider, max_delay_seconds=10.0)

        _ask(gateway)

        assert sleeps == [4, 10.0]

    def test_default_policy_makes_a_single_attempt(self) -> None:
        provider = _ScriptedProvider(_transient(), "unused")
        gateway = AIGateway(
            embedding_provider=LocalEmbeddingProvider.__new__(LocalEmbeddingProvider),
            completion_provider=provider,  # type: ignore[arg-type]
        )

        with pytest.raises(AIUnavailableError):
            _ask(gateway)

        assert provider.calls == 1


class TestErrorNormalization:
    def test_an_unexpected_provider_exception_becomes_a_retryable_ai_error(self) -> None:
        provider = _ScriptedProvider(RuntimeError("driver exploded: password=hunter2"))
        gateway, _ = _gateway(provider, attempts=1)

        with pytest.raises(AIUnavailableError) as caught:
            _ask(gateway)

        assert caught.value.reason == AIUnavailableError.PROVIDER_ERROR
        assert "hunter2" not in str(caught.value)
        assert "hunter2" not in json.dumps(caught.value.details)

    def test_other_domain_errors_are_not_swallowed(self) -> None:
        provider = _ScriptedProvider(NotFoundError("missing"))
        gateway, _ = _gateway(provider)

        with pytest.raises(NotFoundError):
            _ask(gateway)

        assert provider.calls == 1

    def test_ai_errors_map_to_503_never_401(self) -> None:
        error = _transient(AIUnavailableError.AUTH_FAILED)

        assert error.http_status == 503
        assert error.code == "ai_unavailable"
        assert error.details["reason"] == AIUnavailableError.AUTH_FAILED


class TestLimiter:
    def _limiter(self, *, max_requests: int, max_wait: float, clock, sleep) -> SlidingWindowLimiter:
        return SlidingWindowLimiter(
            max_requests, window_seconds=60.0, max_wait_seconds=max_wait, clock=clock, sleep=sleep
        )

    def test_requests_under_the_limit_pass_immediately(self) -> None:
        now = [0.0]
        limiter = self._limiter(
            max_requests=3, max_wait=5.0, clock=lambda: now[0], sleep=lambda s: None
        )

        for _ in range(3):
            limiter.acquire()

    def test_exceeding_the_limit_beyond_the_wait_budget_is_rejected_as_rate_limited(self) -> None:
        now = [0.0]
        limiter = self._limiter(
            max_requests=2, max_wait=5.0, clock=lambda: now[0], sleep=lambda s: None
        )
        limiter.acquire()
        limiter.acquire()

        with pytest.raises(AIUnavailableError) as caught:
            limiter.acquire()

        assert caught.value.reason == AIUnavailableError.RATE_LIMITED
        assert caught.value.retry_after_seconds == pytest.approx(60.0)

    def test_capacity_returns_after_the_window_passes(self) -> None:
        now = [0.0]
        limiter = self._limiter(
            max_requests=1, max_wait=5.0, clock=lambda: now[0], sleep=lambda s: None
        )
        limiter.acquire()
        now[0] = 61.0

        limiter.acquire()

    def test_the_gateway_applies_the_limiter_to_every_attempt(self) -> None:
        provider = _ScriptedProvider("a", "b", "c")
        now = [0.0]
        limiter = self._limiter(
            max_requests=2, max_wait=1.0, clock=lambda: now[0], sleep=lambda s: None
        )
        gateway = AIGateway(
            embedding_provider=LocalEmbeddingProvider.__new__(LocalEmbeddingProvider),
            completion_provider=provider,  # type: ignore[arg-type]
            limiter=limiter,
        )

        _ask(gateway)
        _ask(gateway)
        with pytest.raises(AIUnavailableError) as caught:
            _ask(gateway)

        assert caught.value.reason == AIUnavailableError.RATE_LIMITED
        assert provider.calls == 2


class TestNoSensitiveLogging:
    def test_prompt_and_response_content_never_reach_the_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = _ScriptedProvider(f"response mentions {_SECRET_FILE_TEXT}")
        gateway, _ = _gateway(provider)

        with caplog.at_level(logging.DEBUG):
            gateway.complete(
                messages=[Message(role="user", content=f"summarize {_SECRET_FILE_TEXT}")],
                context=f"context {_SECRET_FILE_TEXT}",
            )

        rendered = "\n".join(
            f"{record.getMessage()} {json.dumps(record.__dict__, default=str)}"
            for record in caplog.records
        )
        assert _SECRET_FILE_TEXT not in rendered
        assert any(record.getMessage() == "ai_completion" for record in caplog.records)

    def test_failures_log_only_the_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = _ScriptedProvider(_transient(AIUnavailableError.AUTH_FAILED))
        gateway, _ = _gateway(provider)

        with caplog.at_level(logging.DEBUG), pytest.raises(AIUnavailableError):
            gateway.complete(messages=[Message(role="user", content=_SECRET_FILE_TEXT)])

        rendered = "\n".join(
            f"{record.getMessage()} {json.dumps(record.__dict__, default=str)}"
            for record in caplog.records
        )
        assert _SECRET_FILE_TEXT not in rendered
        assert AIUnavailableError.AUTH_FAILED in rendered


class TestRecommend:
    def _context(self) -> AIContext:
        return AIContext(
            user_intent="Organize Nike files",
            candidates=(
                AIContextFile(ref="f1", name="banner.psd", extracted_text=_SECRET_FILE_TEXT),
            ),
        )

    def test_returns_a_validated_non_authoritative_recommendation(self) -> None:
        response = json.dumps(
            {
                "intent": "rename",
                "recommendations": [
                    {
                        "kind": "rename",
                        "target_ref": "f1",
                        "proposed_name": "Nike_Banner_v01.psd",
                        "reason": "convention",
                        "confidence": 0.9,
                    }
                ],
            }
        )
        gateway, _ = _gateway(_ScriptedProvider(response))

        result = gateway.recommend(self._context(), system_instructions="Recommend.")

        assert result.actions[0].target_ref == "f1"
        assert result.requires_confirmation is True
        assert result.authoritative is False

    def test_a_response_that_is_not_json_is_a_non_retryable_invalid_response(self) -> None:
        provider = _ScriptedProvider("Sure! I renamed your files.", "second attempt")
        gateway, _ = _gateway(provider)

        with pytest.raises(AIUnavailableError) as caught:
            gateway.recommend(self._context(), system_instructions="Recommend.")

        assert caught.value.reason == AIUnavailableError.INVALID_RESPONSE
        assert caught.value.retryable is False
        assert provider.calls == 1

    def test_actions_targeting_refs_the_model_invented_are_dropped(self) -> None:
        response = json.dumps(
            {
                "recommendations": [
                    {"kind": "archive", "target_ref": "drive-file-id-9999", "confidence": 0.9}
                ]
            }
        )
        gateway, _ = _gateway(_ScriptedProvider(response))

        result = gateway.recommend(self._context(), system_instructions="Recommend.")

        assert result.actions == ()
        assert len(result.rejected) == 1

    def test_the_request_keeps_file_content_out_of_the_system_role(self) -> None:
        provider = _ScriptedProvider(json.dumps({"recommendations": []}))
        gateway, _ = _gateway(provider)

        gateway.recommend(self._context(), system_instructions="Recommend.")

        messages = provider.received[0]["messages"]
        assert messages[0].role == "system"
        assert _SECRET_FILE_TEXT not in messages[0].content
        assert _SECRET_FILE_TEXT in messages[-1].content
        assert "<untrusted_data" in messages[-1].content


class TestGatewaySurface:
    def test_exposes_reasoning_capabilities_only(self) -> None:
        public = {name for name in dir(AIGateway) if not name.startswith("_")}

        assert public == {
            "complete",
            "completion_model_name",
            "completion_provider_name",
            "embed",
            "embedding_model_name",
            "embedding_model_version",
            "recommend",
            "with_completion_provider",
        }

    def test_the_stub_provider_still_reports_its_identity(self) -> None:
        gateway = AIGateway(
            embedding_provider=LocalEmbeddingProvider.__new__(LocalEmbeddingProvider),
            completion_provider=ExtractiveCompletionProvider(),
        )

        assert gateway.completion_provider_name == "extractive_fallback"
