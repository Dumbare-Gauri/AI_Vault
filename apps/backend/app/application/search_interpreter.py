import json
import re
import uuid
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared import AIUnavailableError, get_logger
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.boundary import UntrustedBlock, build_guarded_messages
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.search import FILE_CATEGORIES, FileQuery

logger = get_logger("app.application.search_interpreter")

_STUB_PROVIDER_NAME = "extractive_fallback"
_CACHE_SIZE = 256
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")
_SORTS = frozenset({"relevance", "largest", "newest", "oldest"})

_SYSTEM_PROMPT = f"""You translate a person's request to FIND FILES into a search filter.
Return ONLY one JSON object, no markdown, with exactly these keys:
{{"text": string or null (names, clients, projects or words the file name or folder should \
contain — keep it short), "categories": [one or more of {sorted(FILE_CATEGORIES)}] or [], \
"extensions": [file extensions without dots] or [], "size_min_bytes": integer or null, \
"size_max_bytes": integer or null, "modified_after": "YYYY-MM-DD" or null, \
"modified_before": "YYYY-MM-DD" or null, "folder": string or null, \
"sort": "relevance"|"largest"|"newest"|"oldest"}}
Only fill a key the request actually states or clearly implies. You never list or invent files —
a separate system runs your filter against the real index."""


class SearchInterpreter:
    """The AI half of search, used only when the deterministic parser placed
    nothing and the request reads like a sentence. The model proposes a
    filter; every field is validated against an allow-list before use, and
    results always come from running that filter against the real index —
    the model never names a file. Answers are cached per organization and
    request text, so a repeated question costs no tokens."""

    _cache: "OrderedDict[tuple[uuid.UUID, str], FileQuery | None]" = OrderedDict()

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._ai_gateway = ai_gateway

    def interpret(self, text: str, *, organization_id: uuid.UUID) -> FileQuery | None:
        key = (organization_id, text.strip().lower())
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        gateway = self._ai_gateway
        provider = resolve_org_completion_provider(self._db, organization_id)
        if provider is not None:
            gateway = gateway.with_completion_provider(provider)
        if gateway.completion_provider_name == _STUB_PROVIDER_NAME:
            return None

        messages = build_guarded_messages(
            system_instructions=_SYSTEM_PROMPT,
            task="Translate this request into a file search filter.",
            untrusted=[UntrustedBlock(ref="request", text=text, max_chars=500)],
        )
        try:
            completion = gateway.complete(messages=messages, max_tokens=300)
            query = _validated(completion.text)
        except AIUnavailableError as exc:
            logger.warning("search_interpretation_unavailable", extra={"reason": exc.reason})
            return None

        self._cache[key] = query
        if len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)
        return query


def _validated(raw: str) -> FileQuery | None:
    try:
        payload = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    def date(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None

    def size(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def text(value: object, limit: int) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""

    categories = tuple(
        c for c in payload.get("categories") or [] if isinstance(c, str) and c in FILE_CATEGORIES
    )
    extensions = tuple(
        e.lower().lstrip(".")
        for e in payload.get("extensions") or []
        if isinstance(e, str) and _EXT_RE.match(e.lower().lstrip("."))
    )
    sort = payload.get("sort") if payload.get("sort") in _SORTS else "relevance"
    query = FileQuery(
        text=text(payload.get("text"), 200),
        categories=categories,
        extensions=extensions,
        size_min=size(payload.get("size_min_bytes")),
        size_max=size(payload.get("size_max_bytes")),
        modified_after=date(payload.get("modified_after")),
        modified_before=date(payload.get("modified_before")),
        folder=text(payload.get("folder"), 255) or None,
        sort=sort,
    )
    understood = [f"{c} files" for c in categories] + [f".{e} files" for e in extensions]
    if query.size_min is not None:
        understood.append(f"at least {query.size_min:,} bytes")
    if query.size_max is not None:
        understood.append(f"at most {query.size_max:,} bytes")
    if query.modified_after:
        understood.append(f"modified after {query.modified_after:%Y-%m-%d}")
    if query.modified_before:
        understood.append(f"modified before {query.modified_before:%Y-%m-%d}")
    if query.folder:
        understood.append(f"in folder “{query.folder}”")
    if query.text:
        understood.append(f"matching “{query.text}”")
    if not understood:
        return None
    return replace(query, understood=tuple(understood))
