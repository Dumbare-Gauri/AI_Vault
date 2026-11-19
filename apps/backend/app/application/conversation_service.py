import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.application.assistant import NOT_FOUND_IN_FILES, STORAGE_ASSISTANT_SYSTEM_PROMPT
from app.application.context_builder_service import CitationCandidate, ContextBuilderService
from app.application.search_service import SearchResult, SearchService
from app.application.storage_context_service import StorageContextService
from app.application.vault_ai import blocks as b
from app.application.vault_ai.assistant import VaultAssistant
from app.application.vault_ai.reasoning import ModelReasoning
from app.application.vault_ai.understanding import Request, understand
from vault_shared import (
    AIUnavailableError,
    ConflictError,
    NotFoundError,
    get_logger,
    get_settings,
)
from vault_shared.ai_gateway import AIGateway, Message
from vault_shared.ai_gateway.boundary import with_boundary_rules
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.db.models import (
    Citation,
    ConnectorProvider,
    Conversation,
    ConversationMessage,
    File,
    MessageRole,
    StorageConnector,
)
from vault_shared.db.repositories import (
    CitationRepository,
    ConversationMessageRepository,
    ConversationRepository,
    FileIntelligenceRepository,
    FileRepository,
    FolderRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.formatting import human_bytes
from vault_shared.metrics import record_assistant_tool_used

_TITLE_MAX_LENGTH = 80
# A chat answer's citation panel needs a short, high-confidence source list,
# not the up-to-20-result set the standalone /search page shows — this caps
# how many files ground a single conversational turn.
_MAX_GROUNDING_RESULTS = 6
_MAX_ON_SCREEN = 5000
# Searched before narrowing to the folder on screen, so a folder's matches
# aren't crowded out by better-ranked files elsewhere.
_MAX_SEARCH_CANDIDATES = 50
# A whole folder or selection: every file is counted, these many are named,
# and the content of the most recent ones is read, within this budget.
_MAX_INVENTORY_NAMES = 200
_MAX_SCOPE_CONTENT_FILES = 15
_SCOPE_CONTEXT_CHARS = 16000
_SUMMARY = re.compile(
    r"\b(summ?ar(y|i[sz]e|i[sz]ing)|overview|describe|tell\s+me\s+about|"
    r"what('?s|\s+is|\s+are)\s+(in|inside))\b",
    re.IGNORECASE,
)
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
class PageContext:
    """What the user is looking at when they ask: the page, and the files on
    it (a filtered list, a selection, or the one file being viewed). Only
    ids — never content — and each is checked against the workspace."""

    page: str
    label: str | None = None
    # The files visible on the page (a filtered list, a search result).
    file_ids: tuple[str, ...] = ()
    # The folder being browsed — or a whole connected folder/drive.
    folder_id: str | None = None
    source_id: str | None = None
    # What the user ticked: always what "these" means when present.
    selected_file_ids: tuple[str, ...] = ()


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
        self._sources = StorageSourceRepository(db)
        self._folders = FolderRepository(db)
        self._connectors = StorageConnectorRepository(db)

    def list_for_user(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        connector_id: uuid.UUID | None = None,
    ) -> list[Conversation]:
        return self._conversations.list_for_user(
            organization_id=organization_id, user_id=user_id, connector_id=connector_id
        )

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
        workspace = self._workspace_of(conversation)
        assistant = VaultAssistant(
            self._db,
            organization_id=organization_id,
            user_id=user_id,
            workspace=workspace,
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
        workspace: StorageConnector,
        page: PageContext | None = None,
    ) -> AssistantTurn:
        """`workspace` is the storage the user is in, already validated
        against the organization. A new conversation is bound to it; an
        existing one must already be bound to the same storage — a
        conversation about Google Drive never continues on this computer,
        and the reverse, so no earlier file reference crosses over."""
        if conversation_id is None:
            conversation = self._conversations.create(
                organization_id=organization_id,
                user_id=user_id,
                title=question[:_TITLE_MAX_LENGTH],
                connector_id=workspace.id,
            )
        else:
            conversation = self.get_owned(
                conversation_id, organization_id=organization_id, user_id=user_id
            )
            if conversation.connector_id is None:
                raise ConflictError(
                    "This conversation is from before storage workspaces and can't continue — "
                    "start a new one."
                )
            if conversation.connector_id != workspace.id:
                raise ConflictError(
                    "This conversation belongs to another storage. Start a new conversation "
                    "here, or switch back to continue it."
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
        state = self._with_current_place(
            dict(conversation.state or {}),
            self._place(page, workspace=workspace, organization_id=organization_id),
        )
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
                workspace=workspace,
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
                workspace=workspace,
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
        workspace: StorageConnector,
        ai_gateway: AIGateway,
        reasoning: ModelReasoning | None,
    ) -> tuple[ConversationMessage, list[Citation]]:
        """Storage questions and requests: answered from AI Vault's own data,
        with changes proposed for the user to confirm."""
        assistant = VaultAssistant(
            self._db,
            organization_id=organization_id,
            user_id=user_id,
            workspace=workspace,
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
        workspace: StorageConnector,
        ai_gateway: AIGateway,
    ) -> tuple[ConversationMessage, list[Citation]]:
        conversation.state = state
        grounding = self._grounding(
            question,
            request,
            state,
            organization_id=organization_id,
            user_id=user_id,
            workspace=workspace,
        )
        if isinstance(grounding, str):
            # Which files the user means is unclear — ask, never guess.
            return (
                self._messages.create(
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content=grounding,
                    tool_name="vault:clarify_scope",
                ),
                [],
            )
        context_bundle = self._context_builder.build(
            grounding.results, question=question, max_chars=grounding.max_chars
        )
        if not context_bundle.citations and not grounding.inventory:
            # Nothing in the connected files to ground an answer in — say so
            # rather than letting the model answer from general knowledge.
            assistant_message = self._messages.create(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=NOT_FOUND_IN_FILES,
                retrieval_method=None,
                provider=None,
                token_usage=None,
                blocks=grounding.blocks,
            )
            return assistant_message, []
        context_text = "\n\n".join(
            part for part in (grounding.inventory, context_bundle.context_text) if part
        )
        blocks = list(grounding.blocks)
        if grounding.inventory:
            read = len(context_bundle.citations)
            blocks.append(
                b.notice(
                    f"Counts and sizes cover all {grounding.total:,} files in {grounding.label}; "
                    f"the content of {read:,} of them was read for this answer."
                    if read < grounding.total
                    else f"Based on the content of all {grounding.total:,} files in "
                    f"{grounding.label}.",
                    tone="info" if read >= grounding.total else "warning",
                )
            )
        completion_messages = self._build_completion_messages(history, question)
        try:
            completion = ai_gateway.complete(
                messages=completion_messages,
                context=context_text or None,
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
            answer_text = _DEGRADED_NOTICE + (context_text or _NO_DEGRADED_CONTEXT)
            provider, token_usage = _STUB_PROVIDER_NAME, None

        retrieval_method = _combined_retrieval_method(context_bundle.citations)
        assistant_message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            retrieval_method=retrieval_method,
            provider=provider,
            token_usage=token_usage,
            blocks=blocks,
        )

        conversation.state = {
            **state,
            "last_result": {
                "label": "the files I used to answer",
                "file_ids": [str(c.file.id) for c in context_bundle.citations],
                "place": (state.get("here") or {}).get("key", ""),
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
        self,
        request: Request,
        state: dict[str, Any],
        *,
        organization_id: uuid.UUID,
        workspace: StorageConnector,
    ) -> list[SearchResult]:
        """ "What does this contract say?" right after a search means the
        files just shown — ground the answer in those, if they are still in
        this workspace's storage."""
        if not request.refers_to_previous:
            return []
        ids = [uuid.UUID(i) for i in (state.get("last_result") or {}).get("file_ids") or []]
        files = self._in_workspace(ids, workspace=workspace, organization_id=organization_id)
        return [
            SearchResult(file=file, score=1.0, retrieval_method="metadata")
            for file in files[:_MAX_GROUNDING_RESULTS]
        ]

    def _in_workspace(
        self, ids: list[uuid.UUID], *, workspace: StorageConnector, organization_id: uuid.UUID
    ) -> list[File]:
        """The files among `ids` that belong to this workspace's storage, in
        the given order. An id from another storage is dropped, never
        reinterpreted."""
        sources = {source.id for source in self._sources.list_for_connector(workspace.id)}
        owned = {
            file.id: file
            for file in self._files.list_owned_by_organization(ids, organization_id=organization_id)
            if file.storage_source_id in sources
        }
        return [owned[file_id] for file_id in ids if file_id in owned]

    def _place(
        self, page: PageContext | None, *, workspace: StorageConnector, organization_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Where the user is asking from, checked against the workspace: the
        folder (or connected folder/drive) on screen, what they selected and
        what is visible. Anything from another storage is dropped, never
        reinterpreted; the label is built here, not taken from the client."""
        if page is None:
            return None
        sources = {source.id: source for source in self._sources.list_for_connector(workspace.id)}
        folder = None
        folder_id = _as_uuid(page.folder_id)
        if folder_id is not None:
            found = self._folders.get_by_id(folder_id)
            if found is not None and found.storage_source_id in sources:
                folder = found
        source_id = folder.storage_source_id if folder else _as_uuid(page.source_id)
        source = sources.get(source_id) if source_id else None
        selected = self._in_workspace(
            _as_uuids(page.selected_file_ids), workspace=workspace, organization_id=organization_id
        )
        visible = (
            []
            if source
            else self._in_workspace(
                _as_uuids(page.file_ids), workspace=workspace, organization_id=organization_id
            )
        )
        label = _place_label(source.name, folder.path if folder else None) if source else None
        return {
            "key": "|".join(
                (page.page, str(source.id) if source else "", str(folder.id) if folder else "")
            ),
            "page": page.page,
            "label": label or page.label,
            "source_id": str(source.id) if source else None,
            "folder_id": str(folder.id) if folder else None,
            "folder_path": folder.path if folder else None,
            "selected_ids": [str(file.id) for file in selected],
            "visible_ids": [str(file.id) for file in visible],
        }

    @staticmethod
    def _with_current_place(state: dict[str, Any], here: dict[str, Any] | None) -> dict[str, Any]:
        """The conversation keeps its history, but what "these" and "here"
        mean is this turn's screen: a result from another screen is dropped,
        so an earlier folder's files never silently answer for this one."""
        key = here["key"] if here else ""
        if here:
            state["here"] = here
        else:
            state.pop("here", None)
        last = state.get("last_result")
        if last is not None and last.get("place", "") != key:
            state.pop("last_result", None)
            state.pop("last_duplicates", None)
        if here and here["visible_ids"] and not here["source_id"]:
            # A results page: the visible list is what "these" means.
            state["last_result"] = {
                "label": here["label"] or "the files on this page",
                "file_ids": here["visible_ids"],
                "place": key,
            }
        return state

    def _grounding(
        self,
        question: str,
        request: Request,
        state: dict[str, Any],
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        workspace: StorageConnector,
    ) -> "_Grounding | str":
        """The files an answer is drawn from, in order of what the user
        means: what they selected; what was just shown; everything in the
        folder they're in (when they say "all", "everything", "this
        folder"); otherwise a search — in that folder first. When they say
        "these" with nothing to point at, a question back instead of a
        guess."""
        here = state.get("here") or {}
        label = here.get("label") or "this page"
        narrowed = bool(here.get("source_id")) and not request.everywhere
        asks_summary = bool(_SUMMARY.search(question))
        selected = self._in_workspace(
            _as_uuids(here.get("selected_ids") or ()),
            workspace=workspace,
            organization_id=organization_id,
        )
        if selected and (request.refers_to_selection or request.whole_place or asks_summary):
            count = len(selected)
            return self._scope_grounding(
                selected, label=f"the {count:,} selected file{'' if count == 1 else 's'}",
                workspace=workspace,
            )  # fmt: skip
        previous = self._files_in_view(
            request, state, organization_id=organization_id, workspace=workspace
        )
        if previous:
            return _Grounding(results=previous)
        if narrowed and request.whole_place:
            source_id = uuid.UUID(here["source_id"])
            files = (
                self._files.list_under_folder(source_id, here["folder_path"])
                if here.get("folder_path")
                else self._files.list_for_source(source_id)
            )
            return self._scope_grounding(files, label=label, workspace=workspace)
        if here and (request.refers_to_selection or asks_summary):
            if narrowed:
                return (
                    f"No files are selected in {label}. Tick the files you mean in the list, "
                    f"or ask about “all files in this folder” and I'll use everything in {label}."
                )
            return (
                "Which files do you mean? Select them on the page, or name them — for "
                "example “summarize my invoices”."
            )
        results = self._search.search(
            question,
            organization_id=organization_id,
            user_id=user_id,
            connector_id=workspace.id,
            limit=_MAX_SEARCH_CANDIDATES if narrowed else _MAX_GROUNDING_RESULTS,
        )
        if not narrowed:
            return _Grounding(results=results)
        source_id = uuid.UUID(here["source_id"])
        prefix = (here.get("folder_path") or "").rstrip("/") + "/"
        in_place = [
            result
            for result in results
            if result.file.storage_source_id == source_id
            and (prefix == "/" or (result.file.path or "").startswith(prefix))
        ]
        if in_place:
            return _Grounding(results=in_place[:_MAX_GROUNDING_RESULTS])
        if not results:
            return _Grounding(results=[])
        return _Grounding(
            results=results[:_MAX_GROUNDING_RESULTS],
            blocks=[
                b.notice(
                    f"Nothing in {label} matched — this answer uses files elsewhere in "
                    "this storage.",
                    tone="warning",
                )
            ],
        )

    def _scope_grounding(
        self, files: list[File], *, label: str, workspace: StorageConnector
    ) -> "_Grounding":
        """A whole set of files: an exact inventory computed here (count,
        size, types, names) plus the content of as many as fit. The answer
        says how many were actually read — never implying all were."""
        total = len(files)
        size = sum(file.size_bytes or 0 for file in files)
        types: dict[str, int] = {}
        for file in files:
            extension = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else "no type"
            types[extension] = types.get(extension, 0) + 1
        listed = "\n".join(
            f"- {file.name} ({human_bytes(file.size_bytes or 0)}) — {file.path}"
            for file in files[:_MAX_INVENTORY_NAMES]
        )
        more = total - min(total, _MAX_INVENTORY_NAMES)
        inventory = (
            f"### Inventory of {label} (exact, computed by AI Vault)\n"
            f"{total:,} files, {human_bytes(size)} in total.\n"
            "By type: "
            + ", ".join(
                f"{kind} {count:,}" for kind, count in sorted(types.items(), key=lambda kv: -kv[1])
            )
            + f"\n{listed}"
            + (f"\n…and {more:,} more files not listed by name." if more else "")
        )
        newest_first = sorted(
            files,
            key=lambda file: file.provider_modified_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        storage = (
            "This computer"
            if workspace.provider == ConnectorProvider.LOCAL_AGENT
            else "Google Drive"
        )
        return _Grounding(
            results=[
                SearchResult(file=file, score=1.0, retrieval_method="metadata")
                for file in newest_first[:_MAX_SCOPE_CONTENT_FILES]
            ],
            inventory=inventory if files else None,
            label=label,
            total=total,
            max_chars=_SCOPE_CONTEXT_CHARS,
            blocks=[
                b.file_list(
                    f"Files in {label}",
                    files,
                    total,
                    lambda file: b.file_item(file, workspace, storage),
                )
            ]
            if files
            else [],
        )

    def _workspace_of(self, conversation: Conversation) -> StorageConnector:
        connector = (
            self._connectors.get_by_id(conversation.connector_id)
            if conversation.connector_id
            else None
        )
        if connector is None:
            raise ConflictError("This conversation has no storage workspace any more.")
        return connector


@dataclass(frozen=True)
class _Grounding:
    results: list[SearchResult]
    # An exact inventory of a whole scope, prepended to the model's context.
    inventory: str | None = None
    label: str = ""
    total: int = 0
    max_chars: int = 8000
    blocks: list[dict[str, Any]] = field(default_factory=list)


def _as_uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def _as_uuids(raw: Iterable[str]) -> list[uuid.UUID]:
    return [value for item in list(raw)[:_MAX_ON_SCREEN] if (value := _as_uuid(item))]


def _place_label(source_name: str, folder_path: str | None) -> str:
    """`C:\\Database\\Reports` on a computer, `My Drive/Reports` on Drive."""
    if not folder_path or folder_path == "/":
        return source_name
    if "\\" in source_name or (len(source_name) >= 2 and source_name[1] == ":"):
        return source_name.rstrip("\\") + "\\" + folder_path.strip("/").replace("/", "\\")
    return f"{source_name.rstrip('/')}/{folder_path.strip('/')}"


def _combined_retrieval_method(citations: list[CitationCandidate]) -> str | None:
    methods = {citation.retrieval_method for citation in citations}
    if not methods:
        return None
    if len(methods) == 1:
        return methods.pop()
    return "both"
