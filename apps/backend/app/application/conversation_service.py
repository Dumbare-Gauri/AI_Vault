import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.assistant import (
    STORAGE_ASSISTANT_SYSTEM_PROMPT,
    TOOL_REGISTRY,
    ToolCall,
    ToolContext,
    ToolSpec,
    classify_intent,
    extract_citable_file_ids,
    render_deterministic_answer,
    render_tool_context,
)
from app.application.context_builder_service import CitationCandidate, ContextBuilderService
from app.application.search_service import SearchService
from app.infrastructure.cache.rate_limit_counter import check_rate_limit
from app.infrastructure.cache.response_cache import get_cached_json, set_cached_json
from vault_shared import AIUnavailableError, NotFoundError, get_logger, get_settings
from vault_shared.ai_gateway import AIGateway, Message
from vault_shared.ai_gateway.boundary import with_boundary_rules
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.db.models import (
    Citation,
    Conversation,
    ConversationMessage,
    File,
    MessageRole,
)
from vault_shared.db.repositories import (
    CitationRepository,
    ConversationMessageRepository,
    ConversationRepository,
    FileRepository,
)
from vault_shared.metrics import record_assistant_tool_used

_TITLE_MAX_LENGTH = 80
# A chat answer's citation panel needs a short, high-confidence source list,
# not the up-to-20-result set the standalone /search page shows — this caps
# how many files ground a single conversational turn.
_MAX_GROUNDING_RESULTS = 6
# `AIGateway.get_ai_gateway()`'s stub adapter — checked so a tool turn with
# no real completion provider configured answers deterministically from the
# tool result directly, rather than routing through the stub's generic
# context echo (see `render_deterministic_answer`).
_STUB_PROVIDER_NAME = "extractive_fallback"
# Shown when the AI reasoning service fails mid-conversation: the storage
# product must keep answering from its own deterministic data instead of
# failing the request (and never leaks the provider's error text).
_DEGRADED_NOTICE = (
    "The AI assistant is temporarily unavailable, so this answer comes directly from your "
    "indexed data without AI summarization.\n\n"
)
_NO_DEGRADED_CONTEXT = "No matching indexed content was found for this question."

logger = get_logger("app.application.conversation_service")


@dataclass(frozen=True)
class AssistantTurn:
    conversation: Conversation
    user_message: ConversationMessage
    assistant_message: ConversationMessage
    citations: list[Citation]
    files_by_id: dict[uuid.UUID, File]


@dataclass(frozen=True)
class ConversationDetail:
    conversation: Conversation
    messages: list[ConversationMessage]
    citations_by_message_id: dict[uuid.UUID, list[Citation]]
    files_by_id: dict[uuid.UUID, File]


class ConversationService:
    """Phase 6's Conversation Framework ("Ask Vault"), extended by ADR-024's
    AI Storage Assistant — a single turn now takes one of two paths:

    - A question the deterministic intent classifier (`classify_intent`)
      recognizes as a storage question (e.g. "how much storage am I
      using?") dispatches to one or more tools wrapping Storage
      Intelligence data directly — no semantic search, no embedding call,
      exact figures.
    - Everything else keeps today's retrieval → context assembly →
      `AIGateway.complete()` path unchanged, now with the same persona/
      injection-defense system prompt prepended (previously this path sent
      no system message at all).

    Deliberately single-turn *reasoning* per the original phase spec
    ("single-turn query/response as the baseline capability"): each call
    retrieves/dispatches fresh from the current question, but bounded
    conversation history is still passed to the completion provider so a
    follow-up reads as part of the same conversation, never another
    organization's or user's history (Conversations are user-scoped, not
    just org-scoped — see `ConversationRepository.get_owned`)."""

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._ai_gateway = ai_gateway
        self._settings = get_settings()
        self._conversations = ConversationRepository(db)
        self._messages = ConversationMessageRepository(db)
        self._citations = CitationRepository(db)
        self._files = FileRepository(db)
        self._search = SearchService(db, ai_gateway=ai_gateway)
        self._context_builder = ContextBuilderService(db)

    def list_for_user(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Conversation]:
        return self._conversations.list_for_user(organization_id=organization_id, user_id=user_id)

    def get_owned(
        self, conversation_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> Conversation:
        conversation = self._conversations.get_owned(
            conversation_id, organization_id=organization_id, user_id=user_id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    def get_detail(
        self, conversation_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> ConversationDetail:
        conversation = self.get_owned(
            conversation_id, organization_id=organization_id, user_id=user_id
        )
        messages = self._messages.list_for_conversation(conversation.id)
        citations = self._citations.list_for_messages([message.id for message in messages])
        citations_by_message_id: dict[uuid.UUID, list[Citation]] = {
            message.id: [] for message in messages
        }
        for citation in citations:
            citations_by_message_id[citation.message_id].append(citation)
        files_by_id = {
            file.id: file
            for file in self._files.list_by_ids(list({c.file_id for c in citations}))
        }
        return ConversationDetail(
            conversation=conversation,
            messages=messages,
            citations_by_message_id=citations_by_message_id,
            files_by_id=files_by_id,
        )

    def ask(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> AssistantTurn:
        if conversation_id is None:
            conversation = self._conversations.create(
                organization_id=organization_id,
                user_id=user_id,
                title=question[:_TITLE_MAX_LENGTH],
            )
        else:
            conversation = self.get_owned(
                conversation_id, organization_id=organization_id, user_id=user_id
            )

        history = self._messages.list_recent_for_conversation(
            conversation.id, limit=self._settings.ai_max_history_messages
        )
        user_message = self._messages.create(
            conversation_id=conversation.id, role=MessageRole.USER, content=question
        )

        # An org's own AI provider config (if any) takes over for this
        # turn's completion calls only — `self._ai_gateway` (the cached,
        # process-wide singleton, and its shared embedding provider) is
        # never mutated; `with_completion_provider` returns a new instance.
        ai_gateway = self._ai_gateway
        org_completion_provider = resolve_org_completion_provider(self._db, organization_id)
        if org_completion_provider is not None:
            ai_gateway = self._ai_gateway.with_completion_provider(org_completion_provider)

        tool_calls = classify_intent(question)[: self._settings.ai_max_tool_calls]
        if tool_calls and not self._check_tool_rate_limit(organization_id, user_id):
            # Degrade to the RAG path rather than raising — the route-level
            # `conversation-ask` rate limiter is already the hard stop for
            # this endpoint; this narrower, service-layer limit's job is
            # capping expensive tool dispatch specifically, so it sheds
            # that work rather than failing the whole request.
            record_assistant_tool_used("rate_limited")
            tool_calls = []

        if tool_calls:
            assistant_message, citations = self._answer_with_tools(
                conversation=conversation,
                history=history,
                question=question,
                tool_calls=tool_calls,
                organization_id=organization_id,
                user_id=user_id,
                ai_gateway=ai_gateway,
            )
        else:
            assistant_message, citations = self._answer_with_search(
                conversation=conversation,
                history=history,
                question=question,
                organization_id=organization_id,
                user_id=user_id,
                ai_gateway=ai_gateway,
            )
            record_assistant_tool_used("none")

        self._conversations.touch(conversation)
        self._db.commit()

        files_by_id = {
            file.id: file
            for file in self._files.list_by_ids(list({c.file_id for c in citations}))
        }
        return AssistantTurn(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            citations=citations,
            files_by_id=files_by_id,
        )

    def _answer_with_tools(
        self,
        *,
        conversation: Conversation,
        history: list[ConversationMessage],
        question: str,
        tool_calls: list[ToolCall],
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        ai_gateway: AIGateway,
    ) -> tuple[ConversationMessage, list[Citation]]:
        ctx = ToolContext(
            db=self._db,
            organization_id=organization_id,
            user_id=user_id,
            ai_gateway=ai_gateway,
        )
        results: list[tuple[str, dict]] = []
        for call in tool_calls:
            spec = TOOL_REGISTRY.get(call.name)
            if spec is None:
                continue

            cached = self._get_cached_tool_result(spec, organization_id, call.args)
            if cached is not None:
                result = cached
            else:
                try:
                    result = spec.handler(ctx, call.args)
                except NotFoundError:
                    result = {"error": "That could not be found."}
                if spec.cacheable:
                    self._set_cached_tool_result(spec, organization_id, call.args, result)

            results.append((call.name, result))
            record_assistant_tool_used(call.name)

        completion_messages = self._build_completion_messages(history, question)

        if ai_gateway.completion_provider_name == _STUB_PROVIDER_NAME:
            text = render_deterministic_answer(results)
            provider = _STUB_PROVIDER_NAME
            token_usage = None
        else:
            context_text = render_tool_context(results)
            try:
                completion = ai_gateway.complete(
                    messages=completion_messages,
                    context=context_text,
                    max_tokens=self._settings.ai_max_output_tokens,
                )
                text = completion.text
                provider = completion.provider
                token_usage = completion.tokens_used
            except AIUnavailableError as exc:
                logger.warning("assistant_degraded_to_deterministic", extra={"reason": exc.reason})
                record_assistant_tool_used("ai_degraded")
                text = _DEGRADED_NOTICE + render_deterministic_answer(results)
                provider = _STUB_PROVIDER_NAME
                token_usage = None

        assistant_message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=text,
            retrieval_method="tool",
            provider=provider,
            token_usage=token_usage,
            tool_name=tool_calls[0].name,
        )

        citations = self._create_tool_citations(assistant_message.id, results)
        return assistant_message, citations

    def _answer_with_search(
        self,
        *,
        conversation: Conversation,
        history: list[ConversationMessage],
        question: str,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        ai_gateway: AIGateway,
    ) -> tuple[ConversationMessage, list[Citation]]:
        results = self._search.search(
            question,
            organization_id=organization_id,
            user_id=user_id,
            limit=_MAX_GROUNDING_RESULTS,
        )
        context_bundle = self._context_builder.build(results)
        completion_messages = self._build_completion_messages(history, question)
        try:
            completion = ai_gateway.complete(
                messages=completion_messages,
                context=context_bundle.context_text or None,
                max_tokens=self._settings.ai_max_output_tokens,
            )
            answer_text, provider, token_usage = (
                completion.text,
                completion.provider,
                completion.tokens_used,
            )
        except AIUnavailableError as exc:
            logger.warning("assistant_degraded_to_retrieval", extra={"reason": exc.reason})
            record_assistant_tool_used("ai_degraded")
            answer_text = _DEGRADED_NOTICE + (context_bundle.context_text or _NO_DEGRADED_CONTEXT)
            provider, token_usage = _STUB_PROVIDER_NAME, None

        retrieval_method = _combined_retrieval_method(context_bundle.citations)
        assistant_message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            retrieval_method=retrieval_method,
            provider=provider,
            token_usage=token_usage,
        )

        citations = [
            self._citations.create(
                message_id=assistant_message.id,
                file_id=candidate.file.id,
                snippet=candidate.snippet,
                confidence=candidate.confidence,
                retrieval_method=candidate.retrieval_method,
            )
            for candidate in context_bundle.citations
        ]
        return assistant_message, citations

    def _build_completion_messages(
        self, history: list[ConversationMessage], question: str
    ) -> list[Message]:
        return (
            [Message(role="system", content=with_boundary_rules(STORAGE_ASSISTANT_SYSTEM_PROMPT))]
            + [Message(role=message.role, content=message.content) for message in history]
            + [Message(role=MessageRole.USER, content=question)]
        )

    def _create_tool_citations(
        self, message_id: uuid.UUID, results: list[tuple[str, dict]]
    ) -> list[Citation]:
        citations = []
        for tool_name, result in results:
            spec = TOOL_REGISTRY.get(tool_name)
            if spec is None or not spec.cite_files:
                continue
            for file_id in extract_citable_file_ids(tool_name, result):
                citations.append(
                    self._citations.create(
                        message_id=message_id,
                        file_id=uuid.UUID(file_id),
                        snippet=None,
                        confidence=1.0,
                        retrieval_method="tool",
                    )
                )
        return citations

    def _check_tool_rate_limit(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        key = f"ratelimit:ai_assistant_tools:{organization_id}:{user_id}"
        return check_rate_limit(
            key, limit=self._settings.ai_tool_rate_limit_per_minute, window_seconds=60
        )

    def _cache_key(self, spec: ToolSpec, organization_id: uuid.UUID, args: dict) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return f"ai_assistant:{spec.name}:{organization_id}:{fingerprint}"

    def _get_cached_tool_result(
        self, spec: ToolSpec, organization_id: uuid.UUID, args: dict
    ) -> dict | None:
        if not spec.cacheable:
            return None
        cached = get_cached_json(self._cache_key(spec, organization_id, args))
        if cached is None:
            return None
        try:
            return json.loads(cached)
        except (ValueError, TypeError):
            # A malformed/legacy cache entry degrades to a live call rather
            # than breaking the turn — same fail-open posture as the cache
            # infra itself.
            return None

    def _set_cached_tool_result(
        self, spec: ToolSpec, organization_id: uuid.UUID, args: dict, result: dict
    ) -> None:
        set_cached_json(
            self._cache_key(spec, organization_id, args),
            json.dumps(result, default=str),
            ttl_seconds=self._settings.ai_tool_cache_ttl_seconds,
        )


def _combined_retrieval_method(citations: list[CitationCandidate]) -> str | None:
    methods = {citation.retrieval_method for citation in citations}
    if not methods:
        return None
    if len(methods) == 1:
        return methods.pop()
    return "both"
