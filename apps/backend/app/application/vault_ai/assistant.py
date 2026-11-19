"""Ask Vault's storage brain: turns an understood request into an answer
built from AI Vault's own data, and turns requests to change storage into
proposals the user confirms.

Everything a list, a count or a total says comes from a query against the
index — never from a model. Nothing here changes storage: an action becomes
a pending proposal in the conversation's state, and only the user's
confirmation (handled by `VaultActionService`, outside the AI layer) carries
it out through the Execution Engine.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.application.search_service import SearchService
from app.application.storage_context_service import StorageContextService, StorageFacts
from app.application.storage_intelligence_service import StorageIntelligenceService
from app.application.vault_ai import blocks as b
from app.application.vault_ai.reasoning import ModelReasoning
from app.application.vault_ai.understanding import Request
from vault_shared.db.models import ConnectorProvider, File, StorageConnector
from vault_shared.db.repositories import DuplicateGroupRepository, FileRepository, FolderRepository
from vault_shared.formatting import human_bytes
from vault_shared.search.file_query import FILE_CATEGORIES, FileQuery

_ALL = b.MAX_RESULT_IDS
_MAX_PENDING = 10
_MAX_HISTORY = 20
_ALREADY_COMPRESSED = frozenset(
    {"mp4", "mov", "mkv", "webm", "avi", "m4v", "jpg", "jpeg", "png", "gif", "webp", "heic",
     "mp3", "m4a", "aac", "ogg", "flac", "zip", "rar", "7z", "gz", "tgz", "bz2", "xz",
     "docx", "xlsx", "pptx", "pdf"}
)  # fmt: skip
# Storage Intelligence's type labels → the search's file categories.
_BREAKDOWN_CATEGORY = {
    "Videos": "video",
    "Images": "image",
    "Audio": "audio",
    "Documents": "document",
    "Spreadsheets": "spreadsheet",
    "Presentations": "presentation",
    "Archives": "archive",
    "Code": "code",
    "PDFs": "pdf",
}
_TYPE_WORDS = {
    "pdf": "PDFs",
    "video": "videos",
    "image": "images",
    "spreadsheet": "spreadsheets",
    "presentation": "presentations",
    "audio": "audio files",
    "archive": "archives",
    "code": "code files",
    "document": "documents",
}


@dataclass
class Reply:
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count:,} {word}"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return f"{count:,} {word[:-1]}ies"
    return f"{count:,} {word}s"


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _in_category(file: File, category: str) -> bool:
    extensions, mime_prefixes = FILE_CATEGORIES.get(category, (frozenset(), ()))
    mime = file.mime_type or ""
    return _extension(file.name) in extensions or any(mime.startswith(p) for p in mime_prefixes)


class VaultAssistant:
    def __init__(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        search: SearchService,
        storage_context: StorageContextService,
        reasoning: ModelReasoning | None,
    ) -> None:
        self._db = db
        self._org = organization_id
        self._user = user_id
        self._search = search
        self._context = storage_context
        self._reasoning = reasoning
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._duplicate_repo = DuplicateGroupRepository(db)
        self._intelligence = StorageIntelligenceService(db)
        self._connected = storage_context.connected(organization_id)
        self._owners = storage_context.source_owners(organization_id)
        self._facts = {f.connector_id: f for f in storage_context.facts(organization_id)}

    # -- entry point ---------------------------------------------------------------

    def handle(self, request: Request, state: dict[str, Any]) -> Reply:
        state = dict(state)
        if not self._connected:
            return Reply(
                "No storage is connected yet. Connect Google Drive or this computer on "
                "Storage Connections, and I'll be able to answer from your files.",
                state=state,
            )
        scoped = self._scope(request)
        if isinstance(scoped, Reply):
            return replace(scoped, state=state)
        handler = getattr(self, f"_{request.kind}")
        reply: Reply = handler(request, state, scoped)
        reply.state = {**state, **reply.state}
        return reply

    # -- scope and helpers -----------------------------------------------------------

    def _scope(self, request: Request) -> StorageConnector | None | Reply:
        if request.scope is None:
            return None
        local = request.scope == "local"
        match = next(
            (c for c in self._connected if (c.provider == ConnectorProvider.LOCAL_AGENT) == local),
            None,
        )
        if match is None:
            where = "this computer" if local else "a cloud storage"
            return Reply(f"I can't look there yet — {where} isn't connected.")
        return match

    def _label(self, connector: StorageConnector | None) -> str:
        if connector is None:
            return "all your storage"
        facts = self._facts.get(connector.id)
        return facts.label if facts else "your storage"

    def _item(self, file: File) -> dict[str, Any]:
        connector = self._owners.get(file.storage_source_id)
        return b.file_item(file, connector, self._label(connector) if connector else None)

    def _stale_notice(self, scope: StorageConnector | None) -> list[dict[str, Any]]:
        facts = (
            [self._facts[scope.id]] if scope and scope.id in self._facts else self._facts.values()
        )
        stale = [f for f in facts if f.stale]
        if not stale:
            return []
        parts = []
        for f in stale:
            when = (
                f"last synced {f.last_synced_at:%d %b, %H:%M} UTC"
                if f.last_synced_at
                else "not synced yet"
            )
            parts.append(f"{f.label} was {when}")
        return [
            b.notice(
                "; ".join(parts) + ". These results come from the index and the storage may "
                "have changed since — rescan for the latest.",
                tone="warning",
            )
        ]

    def _remember_result(self, state: dict[str, Any], files: list[File], label: str) -> None:
        state["last_result"] = {
            "label": label,
            "file_ids": [str(f.id) for f in files[: b.MAX_RESULT_IDS]],
        }

    def _previous(self, state: dict[str, Any]) -> list[File]:
        ids = (state.get("last_result") or {}).get("file_ids") or []
        return self._owned([uuid.UUID(i) for i in ids])

    def _owned(self, ids: list[uuid.UUID]) -> list[File]:
        """Only files of this organization's connected storage — ids from
        state are re-checked every time, never trusted."""
        files = self._files.list_by_ids(ids)
        order = {file_id: n for n, file_id in enumerate(ids)}
        return sorted(
            (f for f in files if f.storage_source_id in self._owners),
            key=lambda f: order.get(f.id, 0),
        )

    def _find(self, request: Request, scope: StorageConnector | None) -> tuple[list[File], int]:
        text = request.search_text or request.query
        filters = FileQuery(
            connector_id=str(scope.id) if scope else None,
            categories=(request.file_type,) if request.file_type else (),
            limit=_ALL,
        )
        outcome = self._search.find(
            text, organization_id=self._org, user_id=self._user, filters=filters, allow_ai=False
        )
        # Files that only *resemble* the request are offered only when nothing
        # matches it — otherwise "find the README files" lists unrelated PDFs.
        exact = [r.file for r in outcome.results if r.retrieval_method != "semantic"]
        if exact:
            similar = len(outcome.results) - len(exact)
            return exact, outcome.total - similar
        return [result.file for result in outcome.results], outcome.total

    def _targets(
        self, request: Request, state: dict[str, Any], scope: StorageConnector | None
    ) -> tuple[list[File], str] | None:
        if request.refers_to_previous or not (request.query or request.file_type):
            previous = self._previous(state)
            if previous and scope is not None:
                # "the ones on my computer" — the previous result, on that storage.
                previous = [f for f in previous if self._owners.get(f.storage_source_id) == scope]
            if previous:
                label = (state.get("last_result") or {}).get("label") or "the files I just showed"
                if scope is not None:
                    label += f" on {self._label(scope)}"
                return previous, label
            if request.refers_to_previous:
                return None
        if request.query or request.file_type:
            files, _total = self._find(request, scope)
            words = request.query or _TYPE_WORDS.get(request.file_type or "", "files")
            return files, f"files matching “{words}”"
        return None

    def _single_storage(self, files: list[File]) -> StorageConnector | None:
        owners = {
            self._owners[f.storage_source_id].id
            for f in files
            if f.storage_source_id in self._owners
        }
        if len(owners) != 1:
            return None
        return self._owners[files[0].storage_source_id]

    def _propose(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        *,
        title: str,
        description: str,
        risk: str,
        confirm_label: str,
        files: list[File],
        preview: dict[str, Any] | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        action_id = uuid.uuid4().hex[:12]
        pending = dict(state.get("pending") or {})
        pending[action_id] = {
            **payload,
            "title": title,
            "proposed_at": datetime.now(UTC).isoformat(),
        }
        state["pending"] = dict(list(pending.items())[-_MAX_PENDING:])
        return {
            "type": "action_proposal",
            "action_id": action_id,
            "kind": payload["kind"],
            "title": title,
            "description": description,
            "risk": risk,
            "confirm_label": confirm_label,
            "affected_count": len(files),
            "items": (items or [self._item(f) for f in files])[: b.FIRST_PAGE],
            "preview": preview,
            "status": "pending",
        }

    def _no_targets(self, verb: str) -> Reply:
        return Reply(
            f"Which files should I {verb}? Ask me to find them first (for example "
            "“find the Blarrow invoices”), then say what to do with them."
        )

    # -- answers -----------------------------------------------------------------------

    def _summary_block(self, facts: list[StorageFacts], *, breakdown: bool) -> dict[str, Any]:
        return {
            "type": "storage_summary",
            "storages": [
                {
                    "label": f.label,
                    "is_local": f.is_local,
                    "indexed_files": f.indexed_files,
                    "indexed_bytes": f.indexed_bytes,
                    "provider_used_bytes": f.provider_used_bytes,
                    "provider_total_bytes": f.provider_total_bytes,
                    "provider_trash_bytes": f.provider_trash_bytes,
                    "potential_savings_bytes": f.potential_savings_bytes,
                    "duplicate_recoverable_bytes": f.duplicate_recoverable_bytes,
                    "last_synced_at": f.last_synced_at.isoformat() if f.last_synced_at else None,
                    "operations": list(f.operations),
                    "breakdown": (
                        sorted(
                            (
                                {"category": k, "bytes": v}
                                for k, v in f.breakdown_by_type_bytes.items()
                            ),
                            key=lambda row: -row["bytes"],
                        )
                        if breakdown
                        else []
                    ),
                }
                for f in facts
            ],
        }

    def _scoped_facts(self, scope: StorageConnector | None) -> list[StorageFacts]:
        if scope is not None:
            return [self._facts[scope.id]] if scope.id in self._facts else []
        return list(self._facts.values())

    def _count_files(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        facts = self._scoped_facts(scope)
        known = [f for f in facts if f.indexed_files is not None]
        if not known:
            return Reply("Nothing has been indexed yet — run a scan first.")
        total = sum(f.indexed_files or 0 for f in known)
        parts = ", ".join(f"{f.indexed_files:,} on {f.label}" for f in known)
        text = f"AI Vault has indexed {_plural(total, 'file')}" + (
            f": {parts}." if len(known) > 1 else f" on {known[0].label}."
        )
        return Reply(
            text, [self._summary_block(facts, breakdown=False), *self._stale_notice(scope)]
        )

    def _storage_summary(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        facts = self._scoped_facts(scope)
        savings = sum(f.potential_savings_bytes or 0 for f in facts)
        duplicates = sum(f.duplicate_recoverable_bytes or 0 for f in facts)
        lines = []
        for f in facts:
            provider = (
                f" The storage itself reports {human_bytes(f.provider_used_bytes)} used"
                + (f" of {human_bytes(f.provider_total_bytes)}" if f.provider_total_bytes else "")
                + "."
                if f.provider_used_bytes is not None
                else ""
            )
            indexed = (
                f"{f.label}: {human_bytes(f.indexed_bytes)} indexed in {f.indexed_files:,} files."
                if f.indexed_files is not None
                else f"{f.label}: not analyzed yet."
            )
            lines.append(indexed + provider)
        text = "\n".join(f"- {line}" for line in lines)
        if savings:
            text += (
                f"\n\nYou could free about **{human_bytes(savings)}**: "
                f"{human_bytes(duplicates)} in duplicate copies"
                + (
                    f" and {human_bytes(savings - duplicates)} in likely temporary files"
                    if savings > duplicates
                    else ""
                )
                + ". Ask me to “show duplicates” or “delete duplicate files” to act on it."
            )
        return Reply(
            text, [self._summary_block(facts, breakdown=False), *self._stale_notice(scope)]
        )

    def _breakdown(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        facts = self._scoped_facts(scope)
        totals: dict[str, int] = {}
        for f in facts:
            for category, size in f.breakdown_by_type_bytes.items():
                totals[category] = totals.get(category, 0) + size
        if not totals:
            return Reply("Nothing has been analyzed yet — run a scan first.")
        top, top_bytes = max(totals.items(), key=lambda kv: kv[1])
        overall = sum(totals.values())
        text = (
            f"**{top}** take the most indexed space in {self._label(scope)}: "
            f"{human_bytes(top_bytes)} of {human_bytes(overall)}."
        )
        result_blocks = [self._summary_block(facts, breakdown=True)]
        search_category = _BREAKDOWN_CATEGORY.get(top)
        if search_category:
            files, total = self._query(
                FileQuery(categories=(search_category,), sort="largest"), scope
            )
            result_blocks.append(b.file_list(f"{top}, largest first", files, total, self._item))
            self._remember_result(state, files, f"your {top.lower()}")
        return Reply(text, result_blocks + self._stale_notice(scope), state)

    def _query(self, query: FileQuery, scope: StorageConnector | None) -> tuple[list[File], int]:
        query = replace(query, connector_id=str(scope.id) if scope else None, limit=_ALL)
        rows, total = self._files.query_files(self._org, query)
        return [file for file, _connector in rows], total

    def _largest_files(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        categories = (request.file_type,) if request.file_type else ()
        files, total = self._query(FileQuery(categories=categories, sort="largest"), scope)
        if not files:
            return Reply("I didn't find any files there.")
        top = files[0]
        text = f"The largest is **{top.name}** ({human_bytes(top.size_bytes or 0)})" + (
            f" on {self._item(top)['storage']}." if scope is None else "."
        )
        self._remember_result(state, files, "your largest files")
        return Reply(
            text,
            [b.file_list("Largest files", files, total, self._item), *self._stale_notice(scope)],
            state,
        )

    def _find_files(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        files, total = self._find(request, scope)
        words = request.query or _TYPE_WORDS.get(request.file_type or "", "") or request.search_text
        where = f" in {self._label(scope)}" if scope else ""
        if not files:
            # The search words aren't repeated back: nothing matched, so there
            # is nothing to show.
            return Reply(f"I couldn't find any matching files{where}.", state=state)
        self._remember_result(state, files, f"the files matching “{words}”")
        return Reply(
            f"Found {_plural(total, 'file')} matching “{words}”{where}.",
            [
                b.file_list(f"Matching “{words}”", files, total, self._item),
                *self._stale_notice(scope),
            ],
            state,
        )

    def _listing(
        self,
        state: dict,
        scope: StorageConnector | None,
        files: list[File],
        total: int,
        *,
        title: str,
        empty: str,
        note: str | None = None,
    ) -> Reply:
        if not files:
            return Reply(empty, state=state)
        self._remember_result(state, files, title.lower())
        return Reply(
            f"Found {_plural(total, 'file')} — {title.lower()}.",
            [b.file_list(title, files, total, self._item, note=note), *self._stale_notice(scope)],
            state,
        )

    def _old_files(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        files, total = self._intelligence.list_old_files(
            self._org, limit=_ALL, offset=0, connector_id=scope.id if scope else None
        )
        return self._listing(
            state, scope, files, total,
            title="Files not changed in over a year", empty="No old files found.",
        )  # fmt: skip

    def _inactive_files(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        files, total = self._intelligence.list_inactive_files(
            self._org, limit=_ALL, offset=0, connector_id=scope.id if scope else None
        )
        return self._listing(
            state, scope, files, total,
            title="Files nobody has used in a long time", empty="No inactive files found.",
        )  # fmt: skip

    def _cleanup_candidates(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        files, total = self._intelligence.list_candidates(
            self._org, limit=_ALL, offset=0, connector_id=scope.id if scope else None
        )
        return self._listing(
            state, scope, files, total,
            title="Files that look temporary",
            empty="Nothing looks like a temporary file.",
            note="Judged by name only (copies, backups, .tmp, logs) — review before removing.",
        )  # fmt: skip

    def _archive_candidates(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        files, total = self._intelligence.list_old_files(
            self._org, limit=_ALL, offset=0, connector_id=scope.id if scope else None
        )
        compressed = sum(1 for f in files if _extension(f.name) in _ALREADY_COMPRESSED)
        note = (
            f"{compressed:,} of these are already compressed (videos, photos, PDFs, zips), so "
            "archiving them saves little space — it mainly tidies them away."
            if compressed
            else "Archiving zips these into one file; how much space that saves depends on "
            "the file types."
        )
        return self._listing(
            state, scope, files, total,
            title="Archive candidates (unchanged for over a year)",
            empty="Nothing has been left untouched for over a year.", note=note,
        )  # fmt: skip

    # -- paging through a stored result ------------------------------------------------

    def file_page(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self._item(f) for f in self._owned([uuid.UUID(i) for i in ids])]

    def duplicate_page(self, group_ids: list[str]) -> list[dict[str, Any]]:
        wanted = [uuid.UUID(i) for i in group_ids]
        members = self._duplicate_repo.list_members_with_files_for_groups(wanted)
        rows = []
        for group_id in wanted:
            group = self._duplicate_repo.get_owned(group_id, organization_id=self._org)
            if group is not None:
                rows.append(self._group_row(group, members.get(group_id, [])))
        return rows

    def _group_row(self, group: Any, members: list[tuple[Any, File]]) -> dict[str, Any]:
        return {
            "id": str(group.id),
            # The evidence: every copy has this size and content fingerprint.
            "fingerprint": (group.checksum or "")[:12] or None,
            "size_bytes": members[0][1].size_bytes if members else None,
            "file_count": group.file_count,
            "recoverable_bytes": group.recoverable_size_bytes,
            "keep_reason": group.recommended_keep_reason,
            "files": [{**self._item(f), "keep": m.is_recommended_keep} for m, f in members],
        }

    # -- duplicates ------------------------------------------------------------------

    def _duplicate_rows(
        self, request: Request, scope: StorageConnector | None
    ) -> list[tuple[Any, list[tuple[Any, File]]]]:
        groups, _total = self._intelligence.list_duplicate_groups(
            self._org, limit=_ALL, offset=0, connector_id=scope.id if scope else None
        )
        members = self._duplicate_repo.list_members_with_files_for_groups([g.id for g in groups])
        rows = [(group, members.get(group.id, [])) for group in groups]
        if request.file_type:
            rows = [
                (g, m) for g, m in rows if any(_in_category(f, request.file_type) for _x, f in m)
            ]
        return rows

    def _duplicates(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        rows = self._duplicate_rows(request, scope)
        kind = _TYPE_WORDS.get(request.file_type or "", "files")
        where = f" in {self._label(scope)}" if scope else ""
        if not rows:
            return Reply(f"No duplicate {kind}{where} — every copy is unique.", state=state)
        recoverable = sum(g.recoverable_size_bytes or 0 for g, _m in rows)
        groups = [self._group_row(group, members) for group, members in rows]
        redundant = [f for _g, members in rows for m, f in members if not m.is_recommended_keep]
        self._remember_result(state, redundant, "the extra duplicate copies")
        state["last_duplicates"] = {"group_ids": [str(g.id) for g, _m in rows]}
        text = (
            f"Found **{_plural(len(rows), 'duplicate group')}** of {kind}{where} — keeping one "
            f"copy of each would free **{human_bytes(recoverable)}**. Say “delete the duplicates” "
            "to move the extra copies to Trash (you'll confirm first)."
        )
        return Reply(
            text,
            [
                b.duplicate_groups(
                    f"Duplicate {kind}",
                    groups,
                    total_groups=len(rows),
                    recoverable_bytes=recoverable,
                    group_ids=[g.id for g, _m in rows],
                ),
                *self._stale_notice(scope),
            ],
            state,
        )

    def _clean_duplicates(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        rows = self._duplicate_rows(request, scope)
        if not rows:
            return Reply("There are no duplicates to clean up.", state=state)
        redundant: list[File] = []
        kept_for: dict[uuid.UUID, File] = {}
        for _group, members in rows:
            files = [f for _m, f in members]
            if request.keep in ("newest", "oldest"):
                dated = sorted(
                    files, key=lambda f: f.provider_modified_at or datetime.min.replace(tzinfo=UTC)
                )
                keep = dated[-1] if request.keep == "newest" else dated[0]
            else:
                keep = next((f for m, f in members if m.is_recommended_keep), files[0])
            copies = [f for f in files if f.id != keep.id]
            redundant += copies
            kept_for.update({copy.id: keep for copy in copies})
        if scope is not None:
            redundant = [f for f in redundant if self._owners.get(f.storage_source_id) == scope]
        freed = sum(f.size_bytes or 0 for f in redundant)
        which = {"newest": "the newest", "oldest": "the oldest"}.get(request.keep or "", "the best")
        description = (
            f"Keeps {which} copy in each of {_plural(len(rows), 'group')} and moves "
            f"{_plural(len(redundant), 'extra copy')} to Trash, freeing about "
            f"{human_bytes(freed)}. Each copy is byte-for-byte identical to the file kept — the "
            "same size and the same content fingerprint — so no content is lost. Everything "
            "stays restorable until the Trash is emptied."
        )
        if request.permanent:
            description += " Permanent deletion happens only when you empty the Trash yourself."
        block = self._propose(
            state,
            {"kind": "trash", "file_ids": [str(f.id) for f in redundant]},
            title=f"Remove {_plural(len(redundant), 'duplicate copy')}",
            description=description,
            risk="medium",
            confirm_label="Move copies to Trash",
            files=redundant,
            items=[
                {**self._item(copy), "duplicate_of": self._item(kept_for[copy.id])}
                for copy in redundant[: b.FIRST_PAGE]
            ],
        )
        return Reply(
            f"Here's what I'll do — {_plural(len(redundant), 'copy')} will go to Trash. "
            "Confirm to go ahead.",
            [block],
            state,
        )

    # -- actions ------------------------------------------------------------------------

    def _trash(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        targets = self._targets(request, state, scope)
        if not targets or not targets[0]:
            return self._no_targets("remove")
        files, label = targets
        description = (
            f"Moves {_plural(len(files), 'file')} "
            f"({human_bytes(sum(f.size_bytes or 0 for f in files))}) "
            "to Trash. They can be restored until the Trash is emptied."
        )
        if request.permanent:
            description += (
                " I only move files to Trash from here — permanent deletion happens when you "
                "empty the Trash, so nothing is lost by mistake."
            )
        block = self._propose(
            state,
            {"kind": "trash", "file_ids": [str(f.id) for f in files]},
            title=f"Move {label} to Trash",
            description=description,
            risk="high" if len(files) > 20 else "medium",
            confirm_label=f"Move {_plural(len(files), 'file')} to Trash",
            files=files,
        )
        return Reply("Please confirm before I remove anything.", [block], state)

    def _restore(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        ids = (state.get("last_trashed") or {}).get("file_ids") or []
        files = self._files.list_owned_by_organization_including_trashed(
            [uuid.UUID(i) for i in ids], organization_id=self._org
        )
        files = [f for f in files if f.trashed]
        if request.refers_to_previous or not files:
            previous = [f for f in self._previous(state) if f.trashed]
            files = previous or files
        if not files:
            return Reply(
                "I don't have any trashed files from this conversation to restore.", state=state
            )
        block = self._propose(
            state,
            {"kind": "restore", "file_ids": [str(f.id) for f in files]},
            title=f"Restore {_plural(len(files), 'file')} from Trash",
            description="Puts them back where they were.",
            risk="low",
            confirm_label="Restore",
            files=files,
        )
        return Reply("Ready to restore — confirm to go ahead.", [block], state)

    def _destination_storage(
        self, state: dict, scope: StorageConnector | None, files: list[File] | None = None
    ) -> StorageConnector | None:
        if files:
            return self._single_storage(files)
        if scope is not None:
            return scope
        return self._connected[0] if len(self._connected) == 1 else None

    def _create_folder(
        self, request: Request, state: dict, scope: StorageConnector | None
    ) -> Reply:
        if not request.name:
            return Reply("What should the new folder be called?", state=state)
        storage = self._destination_storage(state, scope)
        if storage is None:
            names = " or ".join(self._label(c) for c in self._connected)
            return Reply(f"Where should I create it — {names}?", state=state)
        block = self._propose(
            state,
            {"kind": "create_folder", "connector_id": str(storage.id), "name": request.name},
            title=f"Create “{request.name}” in {self._label(storage)}",
            description="Creates the folder at the top level.",
            risk="low",
            confirm_label="Create folder",
            files=[],
        )
        return Reply(f"I'll create “{request.name}” in {self._label(storage)}.", [block], state)

    def _existing_folder(self, storage: StorageConnector, name: str) -> Any:
        matches = self._folders.search_for_connector(storage.id, query=name, limit=20)
        return next((f for f in matches if f.name.lower() == name.lower()), None)

    def _move(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        targets = self._targets(request, state, scope)
        if not targets or not targets[0]:
            return self._no_targets("move")
        files, label = targets
        storage = self._single_storage(files)
        if storage is None:
            names = " or ".join(self._label(c) for c in self._connected)
            return Reply(
                f"These files are in more than one storage ({names}) — say which, for example "
                "“move the ones on my computer into …”.",
                state=state,
            )
        destination: dict[str, Any]
        if request.name:
            folder = self._existing_folder(storage, request.name)
            destination = (
                {"folder_id": str(folder.id), "name": folder.name}
                if folder
                else {"create": request.name, "name": request.name}
            )
        else:
            last = state.get("last_folder") or {}
            if not last or last.get("connector_id") != str(storage.id):
                return Reply("Which folder should they go into?", state=state)
            destination = {"folder_id": last["folder_id"], "name": last["name"]}
        new = " (new folder)" if "create" in destination else ""
        block = self._propose(
            state,
            {
                "kind": "move",
                "connector_id": str(storage.id),
                "file_ids": [str(f.id) for f in files],
                "destination": destination,
            },
            title=f"Move {label} into “{destination['name']}”{new}",
            description=f"Moves {_plural(len(files), 'file')} in {self._label(storage)}.",
            risk="medium",
            confirm_label=f"Move {_plural(len(files), 'file')}",
            files=files,
        )
        return Reply("Here's the move — confirm to go ahead.", [block], state)

    def _archive(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        targets = self._targets(request, state, scope)
        if not targets or not targets[0]:
            return self._no_targets("archive")
        files, label = targets
        storage = self._single_storage(files)
        if storage is None:
            return Reply("Archive one storage at a time — which one?", state=state)
        if not self._facts[storage.id].can_archive:
            return Reply(
                f"Zipping files on {self._label(storage)} isn't supported yet. I can move them "
                "into a folder or to Trash instead.",
                state=state,
            )
        size = sum(f.size_bytes or 0 for f in files)
        compressed = sum(1 for f in files if _extension(f.name) in _ALREADY_COMPRESSED)
        description = (
            f"Zips {_plural(len(files), 'file')} ({human_bytes(size)}) into one archive in "
            f"{self._label(storage)}, checks it, and keeps the originals."
        )
        if compressed:
            description += (
                f" {compressed:,} are already compressed, so the archive won't be much smaller."
            )
        block = self._propose(
            state,
            {
                "kind": "archive",
                "connector_id": str(storage.id),
                "file_ids": [str(f.id) for f in files],
            },
            title=f"Archive {label}",
            description=description,
            risk="medium",
            confirm_label="Create archive",
            files=files,
        )
        return Reply("Confirm and I'll build the archive.", [block], state)

    def _organize(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        targets = self._targets(request, state, scope)
        if not targets or not targets[0]:
            return self._no_targets("organize")
        files, label = targets
        storage = self._single_storage(files)
        if storage is None:
            return Reply(
                "These files are in more than one storage — organize one at a time "
                "(say “organize my Blarrow files on my computer”).",
                state=state,
            )
        files = files[:500]
        root = request.name or (request.query.title() if request.query else "Organized")
        plan = self._reasoning.organize(files, root=root) if self._reasoning else None
        if plan is None:
            plan = _group_by_type(files, root=root)
        by_id = {str(f.id): f for f in files}
        folders = [
            {
                "name": folder["name"],
                "reason": folder.get("reason"),
                "confidence": folder.get("confidence"),
                "count": len(folder["file_ids"]),
                "items": [self._item(by_id[i]) for i in folder["file_ids"][:10]],
            }
            for folder in plan["folders"]
        ]
        unsure = [by_id[i] for i in plan["unsure"]]
        moving = sum(len(f["file_ids"]) for f in plan["folders"])
        block = self._propose(
            state,
            {
                "kind": "organize",
                "connector_id": str(storage.id),
                "root": plan["root"],
                "folders": [
                    {"name": f["name"], "file_ids": f["file_ids"]} for f in plan["folders"]
                ],
            },
            title=f"Organize {label} into “{plan['root']}”",
            description=(
                f"Creates “{plan['root']}” with {_plural(len(plan['folders']), 'folder')} in "
                f"{self._label(storage)} and moves {_plural(moving, 'file')} into them. "
                + (
                    f"{_plural(len(unsure), 'file')} I wasn't sure about stay where they are. "
                    if unsure
                    else ""
                )
                + f"Grouped by {plan['basis']}. Nothing is renamed or deleted."
            ),
            risk="medium",
            confirm_label="Organize",
            files=[by_id[i] for f in plan["folders"] for i in f["file_ids"]],
            preview={
                "root": plan["root"],
                "folders": folders,
                "unsure": [self._item(f) for f in unsure[:10]],
                "unsure_count": len(unsure),
                "folders_created": len(plan["folders"]) + 1,
                "files_moved": moving,
                "files_renamed": 0,
                "files_deleted": 0,
            },
        )
        return Reply(
            f"Here's the structure I'd use for {label}. Review it and confirm to apply.",
            [block],
            state,
        )

    def _rename(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        targets = self._targets(request, state, scope)
        if not targets or not targets[0]:
            return self._no_targets("rename")
        files, label = targets
        if self._reasoning is None:
            return Reply(
                "Suggesting better names needs an AI model — add one under Organization → AI "
                "Provider. You can still rename a single file from its page.",
                state=state,
            )
        renames = self._reasoning.rename(files[:50])
        if not renames:
            return Reply(
                "The names already look clear — I have nothing better to suggest.", state=state
            )
        by_id = {str(f.id): f for f in files}
        block = self._propose(
            state,
            {"kind": "rename", "renames": renames},
            title=f"Rename {_plural(len(renames), 'file')}",
            description="\n".join(
                f"{by_id[r['file_id']].name} → {r['new_name']}" for r in renames[:15]
            ),
            risk="medium",
            confirm_label=f"Rename {_plural(len(renames), 'file')}",
            files=[by_id[r["file_id"]] for r in renames],
        )
        return Reply(f"Suggested names for {label} — confirm to apply them.", [block], state)

    def _what_changed(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        history = state.get("history") or []
        if not history:
            return Reply("I haven't changed anything in this conversation yet.", state=state)
        results = [
            {
                "type": "action_result",
                "action_id": entry["action_id"],
                "title": entry["title"],
                "steps": entry["steps"],
            }
            for entry in history[-_MAX_HISTORY:]
        ]
        return Reply(
            f"In this conversation I carried out {_plural(len(history), 'change')} — here's "
            "what the storage confirmed for each:",
            results,
            state,
        )

    def _ask(self, request: Request, state: dict, scope: StorageConnector | None) -> Reply:
        raise AssertionError("questions about file content are answered by ConversationService")


def _group_by_type(files: list[File], *, root: str) -> dict[str, Any]:
    """The no-AI organization: one folder per kind of file. Honest about its
    basis, and leaves nothing out — every file has a type."""
    names = {
        "pdf": "Documents",
        "document": "Documents",
        "spreadsheet": "Spreadsheets",
        "presentation": "Presentations",
        "image": "Images",
        "video": "Videos",
        "audio": "Audio",
        "archive": "Archives",
        "code": "Code",
    }
    folders: dict[str, list[str]] = {}
    for file in files:
        category = next((c for c in names if _in_category(file, c)), None)
        folders.setdefault(names.get(category or "", "Other"), []).append(str(file.id))
    return {
        "root": root,
        "basis": "file type",
        "folders": [{"name": name, "file_ids": ids} for name, ids in sorted(folders.items())],
        "unsure": [],
    }
