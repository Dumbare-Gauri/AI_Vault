import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.application.assistant import NOT_FOUND_IN_FILES, STORAGE_ASSISTANT_SYSTEM_PROMPT
from app.application.context_builder_service import CitationCandidate, ContextBuilderService
from app.application.search_service import SearchResult, SearchService
from app.application.storage_context_service import StorageContextService
from app.application.vault_ai.assistant import VaultAssistant
from app.application.vault_ai.reasoning import ModelReasoning
from app.application.vault_ai.understanding import Request, understand
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
    FileIntelligenceRepository,
    FileRepository,
)
from vault_shared.metrics import record_assistant_tool_used

_TITLE_MAX_LENGTH = 80
# A chat answer's citation panel needs a short, high-confidence source list,
# not the up-to-20-result set the standalone /search page shows — this caps
# how many files ground a single conversational turn.
_MAX_GROUNDING_RESULTS = 6
# `AIGateway.get_ai_gateway()`'s stub adapter — with no real model
# configured, nothing that needs one (classifying, grouping) is attempted.
_STUB_PROVIDER_NAME = "extractive_fallback"
# A request the fixed rules didn't recognize but that reads like an
# instruction — worth asking the model what it means.
_LOOKS_LIKE_COMMAND = re.compile(
    r"^\s*(please\s+)?(put|group|gather|collect|bundle|sort|clear|consolidate|merge|combine|"
    r"split|separate|file\s+away|set\s+up|build|tidy|clean|declutter|make|get)\b",
    re.IGNORECASE,
)
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

    def __init__(
        self,
        db: Session,
        *,
        ai_gateway: AIGateway,
        storage_context: StorageContextService | None = None,
    ) -> None:
        self._db = db
        self._ai_gateway = ai_gateway
        self._storage_context = storage_context or StorageContextService(db)
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
            file.id: file for file in self._files.list_by_ids(list({c.file_id for c in citations}))
        }
        return ConversationDetail(
            conversation=conversation,
            messages=messages,
            citations_by_message_id=citations_by_message_id,
            files_by_id=files_by_id,
        )

    def result_page(
        self,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        block_index: int,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """One page of a list an answer showed — every result stays
        reachable, not just the first page that came with the message."""
        conversation = self.get_owned(
            conversation_id, organization_id=organization_id, user_id=user_id
        )
        message = next(
            (
                m
                for m in self._messages.list_for_conversation(conversation.id)
                if m.id == message_id
            ),
            None,
        )
        if message is None or not 0 <= block_index < len(message.blocks or []):
            raise NotFoundError("That result is no longer available.")
        block = message.blocks[block_index]
        assistant = VaultAssistant(
            self._db,
            organization_id=organization_id,
            user_id=user_id,
            search=self._search,
            storage_context=self._storage_context,
            reasoning=None,
        )
        if block.get("type") == "duplicate_groups":
            ids = block.get("group_ids") or []
            return {
                "total": len(ids),
                "groups": assistant.duplicate_page(ids[offset : offset + limit]),
            }
        ids = block.get("file_ids") or []
        return {"total": len(ids), "items": assistant.file_page(ids[offset : offset + limit])}

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

        has_model = ai_gateway.completion_provider_name != _STUB_PROVIDER_NAME
        reasoning = (
            ModelReasoning(ai_gateway, summaries=FileIntelligenceRepository(self._db))
            if has_model
            else None
        )
        state = dict(conversation.state or {})
        request = understand(question)
        if request.kind == "ask" and reasoning and _LOOKS_LIKE_COMMAND.search(question):
            request = reasoning.classify(question, request)

        if request.kind != "ask":
            assistant_message, citations = self._answer_with_vault(
                conversation=conversation,
                request=request,
                state=state,
                organization_id=organization_id,
                user_id=user_id,
                ai_gateway=ai_gateway,
                reasoning=reasoning,
            )
        else:
            assistant_message, citations = self._answer_with_search(
                conversation=conversation,
                history=history,
                question=question,
                request=request,
                state=state,
                organization_id=organization_id,
                user_id=user_id,
                ai_gateway=ai_gateway,
            )
        record_assistant_tool_used(request.kind)

        self._conversations.touch(conversation)
        self._db.commit()

        files_by_id = {
            file.id: file for file in self._files.list_by_ids(list({c.file_id for c in citations}))
        }
        return AssistantTurn(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            citations=citations,
            files_by_id=files_by_id,
        )

    def _answer_with_vault(
        self,
        *,
        conversation: Conversation,
        request: Request,
        state: dict[str, Any],
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        ai_gateway: AIGateway,
        reasoning: ModelReasoning | None,
    ) -> tuple[ConversationMessage, list[Citation]]:
        """Storage questions and requests: answered from AI Vault's own data,
        with changes proposed for the user to confirm."""
        assistant = VaultAssistant(
            self._db,
            organization_id=organization_id,
            user_id=user_id,
            search=SearchService(self._db, ai_gateway=ai_gateway),
            storage_context=self._storage_context,
            reasoning=reasoning,
        )
        reply = assistant.handle(request, state)
        conversation.state = reply.state
        message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=reply.text,
            retrieval_method="tool",
            tool_name=f"vault:{request.kind}"[:50],
            blocks=reply.blocks,
        )
        return message, []

    def _answer_with_search(
        self,
        *,
        conversation: Conversation,
        history: list[ConversationMessage],
        question: str,
        request: Request,
        state: dict[str, Any],
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        ai_gateway: AIGateway,
    ) -> tuple[ConversationMessage, list[Citation]]:
        results = self._files_in_view(request, state, organization_id=organization_id)
        if not results:
            results = self._search.search(
                question,
                organization_id=organization_id,
                user_id=user_id,
                limit=_MAX_GROUNDING_RESULTS,
            )
        context_bundle = self._context_builder.build(results, question=question)
        if not context_bundle.citations:
            # Nothing in the connected files to ground an answer in — say so
            # rather than letting the model answer from general knowledge.
            assistant_message = self._messages.create(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=NOT_FOUND_IN_FILES,
                retrieval_method=None,
                provider=None,
                token_usage=None,
            )
            return assistant_message, []
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

        conversation.state = {
            **state,
            "last_result": {
                "label": "the files I used to answer",
                "file_ids": [str(c.file.id) for c in context_bundle.citations],
            },
        }
        citations = [
            self._citations.create(
                message_id=assistant_message.id,
                file_id=candidate.file.id,
                snippet=candidate.snippet,
                confidence=candidate.confidence,
                retrieval_method=candidate.retrieval_method,
                page_number=candidate.page_number,
                passage_index=candidate.passage_index,
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

    def _files_in_view(
        self, request: Request, state: dict[str, Any], *, organization_id: uuid.UUID
    ) -> list[SearchResult]:
        """ "What does this contract say?" right after a search means the
        files just shown — ground the answer in those."""
        if not request.refers_to_previous:
            return []
        ids = [uuid.UUID(i) for i in (state.get("last_result") or {}).get("file_ids") or []]
        files = self._files.list_owned_by_organization(
            ids[:_MAX_GROUNDING_RESULTS], organization_id=organization_id
        )
        return [SearchResult(file=file, score=1.0, retrieval_method="metadata") for file in files]


def _combined_retrieval_method(citations: list[CitationCandidate]) -> str | None:
    methods = {citation.retrieval_method for citation in citations}
    if not methods:
        return None
    if len(methods) == 1:
        return methods.pop()
    return "both"
