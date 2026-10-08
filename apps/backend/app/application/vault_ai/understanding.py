"""What the user is asking AI Vault to do, read deterministically.

Most storage requests ("show every duplicate", "move them into Finance",
"how much can I recover?") have a fixed shape, so they are recognized here
without spending a model call. Only what this can't place becomes `ask` —
answered from file content — or is handed to the model to classify into the
same `Request` shape (see `model_intent.py`), which is validated just the same.
"""

import re
from dataclasses import dataclass, replace

KINDS = frozenset(
    {
        "what_changed",
        "clean_duplicates",
        "duplicates",
        "create_folder",
        "move",
        "rename",
        "organize",
        "restore",
        "archive_candidates",
        "archive",
        "trash",
        "breakdown",
        "largest_files",
        "count_files",
        "old_files",
        "inactive_files",
        "cleanup_candidates",
        "storage_summary",
        "find_files",
        "ask",
    }
)
# Requests that change storage — always proposed for confirmation first.
ACTION_KINDS = frozenset(
    {
        "clean_duplicates",
        "create_folder",
        "move",
        "rename",
        "organize",
        "restore",
        "archive",
        "trash",
    }
)


@dataclass(frozen=True)
class Request:
    kind: str
    scope: str | None = None  # "local" | "drive" | None = every connected storage
    file_type: str | None = None
    query: str = ""  # the subject, e.g. "Blarrow"
    search_text: str = ""  # the request minus verbs/pronouns, for the file-query parser
    refers_to_previous: bool = False
    name: str | None = None  # folder name to create or move into
    destination_is_last_folder: bool = False
    keep: str | None = None  # "newest" | "oldest" — which duplicate copy to keep
    permanent: bool = False


_FLAGS = re.IGNORECASE

_LOCAL_SCOPE = re.compile(
    r"\b(local(\s+storage|\s+files|\s+disk)?|my\s+(computer|laptop|pc|machine|disk)|"
    r"this\s+(computer|laptop|pc|machine)|hard\s+drive|external\s+(drive|disk)|ssd|"
    r"[a-z]:\\)",
    _FLAGS,
)
_DRIVE_SCOPE = re.compile(r"\b(google\s+drive|g-?drive|my\s+drive|drive)\b", _FLAGS)

_FILE_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("pdf", re.compile(r"\bpdfs?\b", _FLAGS)),
    ("video", re.compile(r"\b(videos?|movies?|clips?|mp4s?|recordings?)\b", _FLAGS)),
    (
        "image",
        re.compile(r"\b(images?|photos?|pictures?|pics|pngs?|jpe?gs?|screenshots?)\b", _FLAGS),
    ),
    ("spreadsheet", re.compile(r"\b(spreadsheets?|excel|sheets|csvs?)\b", _FLAGS)),
    ("presentation", re.compile(r"\b(presentations?|slides?|decks?|ppts?|powerpoints?)\b", _FLAGS)),
    ("audio", re.compile(r"\b(audio|music|songs?|mp3s?|podcasts?)\b", _FLAGS)),
    ("archive", re.compile(r"\b(zips?|zip\s+files|rar|7z)\b", _FLAGS)),
    ("code", re.compile(r"\b(code|source\s+files|scripts?)\b", _FLAGS)),
    ("document", re.compile(r"\b(documents?|docs?|word\s+files?)\b", _FLAGS)),
]

_REFERENCE = re.compile(
    r"\b(them|these|those|it|this\s+one|that\s+one|the\s+results?|the\s+ones|those\s+ones)\b",
    _FLAGS,
)
_THERE = re.compile(r"\b(there|into\s+it|in\s+it|that\s+folder|this\s+folder)\b", _FLAGS)

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "what_changed",
        re.compile(
            r"what\s+(did|have)\s+you\s+(change|changed|do|done)|show\s+(me\s+)?what\s+you\s+"
            r"(changed|did)|what\s+changed",
            _FLAGS,
        ),
    ),
    (
        "clean_duplicates",
        re.compile(
            r"\b(delete|remove|clean(\s+up)?|get\s+rid\s+of|trash|clear|dedupe)\b.*\b(duplicat\w*|copies)\b|"
            r"\bkeep\s+(only\s+)?the\s+(newest|latest|most\s+recent|oldest|original|first)\b",
            _FLAGS,
        ),
    ),
    ("duplicates", re.compile(r"duplicat|identical\s+(files|copies)", _FLAGS)),
    ("move", re.compile(r"\bmove\b", _FLAGS)),
    ("create_folder", re.compile(r"\b(create|make|add|new)\b.*\bfolders?\b", _FLAGS)),
    ("rename", re.compile(r"\brename\b", _FLAGS)),
    (
        "organize",
        re.compile(
            r"\b(organi[sz]e|tidy(\s+up)?|sort\s+out|arrange|restructure|put\s+.*\s+in\s+order)\b",
            _FLAGS,
        ),
    ),
    ("restore", re.compile(r"\b(restore|undelete|bring\s+back|un-?trash)\b", _FLAGS)),
    (
        "archive_candidates",
        re.compile(
            r"\b(can|could|should)\s+i\s+(safely\s+)?archive\b|archive\s+candidates|"
            r"what\s+(can|should)\s+(i|be)\s+archived?",
            _FLAGS,
        ),
    ),
    ("archive", re.compile(r"\b(archive|zip|compress|shrink)\b", _FLAGS)),
    ("trash", re.compile(r"\b(delete|remove|trash|bin|get\s+rid\s+of|erase)\b", _FLAGS)),
    (
        "largest_files",
        re.compile(
            r"\b(largest|biggest|heaviest)\b|\bfiles?\b.*\b(using|taking|consuming)\b.*\bmost\b",
            _FLAGS,
        ),
    ),
    (
        "breakdown",
        re.compile(
            r"\b(consuming|taking|using|eating)\s+(up\s+)?(the\s+)?most\s+(space|storage)|"
            r"break\s*down|by\s+(file\s+)?type|what\s+kind\s+of\s+files",
            _FLAGS,
        ),
    ),
    ("count_files", re.compile(r"\bhow\s+many\s+(files|documents|items)\b", _FLAGS)),
    (
        "inactive_files",
        re.compile(
            r"\b(inactive|unused|dormant)\b|(not|never)\s+(been\s+)?(opened|used|viewed)", _FLAGS
        ),
    ),
    ("old_files", re.compile(r"\b(old|oldest|stale|outdated)\b", _FLAGS)),
    (
        "cleanup_candidates",
        re.compile(
            r"\b(temporary|temp|junk|clutter)\b|clean\s*-?\s*up\s+candidates|"
            r"what\s+should\s+i\s+(clean|delete|review)",
            _FLAGS,
        ),
    ),
    (
        "storage_summary",
        re.compile(
            r"\b(storage|space|recover|reclaim|savings?|free\s+up|quota)\b|how\s+much", _FLAGS
        ),
    ),
    (
        "find_files",
        re.compile(
            r"^\s*(find|show|list|where|search|locate|get|give\s+me|which|what\s+files|display|open)\b|"
            r"\b(files?|folders?)\s+(about|for|related\s+to|from|named|called)\b",
            _FLAGS,
        ),
    ),
]

_FOLDER_NAME = re.compile(
    r"folder\s+(?:called|named|titled)?\s*[\"'“]?(?P<name>[^\"'”]+?)[\"'”]?\s*[.!?]?\s*$", _FLAGS
)
_MOVE_DESTINATION = re.compile(
    r"\b(?:into|to|in)\s+(?:a\s+|the\s+)?(?:new\s+)?(?:folder\s+)?(?:called|named)\s+"
    r"[\"'“]?(?P<name>[^\"'”]+?)[\"'”]?\s*[.!?]?\s*$|"
    r"\b(?:into|to|in)\s+(?:the\s+|my\s+)?[\"'“]?(?P<name2>[^\"'”]+?)[\"'”]?\s+folder\b|"
    r"\b(?:into|to)\s+[\"'“]?(?P<name3>[A-Z0-9][^\"'”]*?)[\"'”]?\s*[.!?]?\s*$",
    _FLAGS,
)

_FILLER = re.compile(
    r"\b(please|can\s+you|could\s+you|would\s+you|i\s+want\s+to|i'?d\s+like\s+to|"
    r"find|show|list|search(\s+for)?|locate|get|give|display|open|organi[sz]e|tidy(\s+up)?|"
    r"sort\s+out|arrange|move|rename|archive|zip|compress|shrink|delete|remove|trash|restore|"
    r"where('?s|\s+is|\s+are)?|which|what|me|my|the|a|an|all|every|any|of|in|on|from\s+my|"
    r"files?|folders?|properly|related\s+to|about|for|that|are|is|i|have|do|stuff|things|"
    r"them|these|those|ones|it|there|please|everything|storage|local|computer|laptop|pc|google|"
    r"drive|disk|hard)\b",
    _FLAGS,
)
_DATE_WORDS = re.compile(
    r"\b(last|this|next)\s+(year|month|week)|\b(19|20)\d{2}\b|\b(today|yesterday)\b", _FLAGS
)


def _scope(question: str) -> str | None:
    if _LOCAL_SCOPE.search(question):
        return "local"
    if _DRIVE_SCOPE.search(question):
        return "drive"
    return None


def _file_type(question: str) -> str | None:
    return next((kind for kind, pattern in _FILE_TYPES if pattern.search(question)), None)


def _subject(question: str) -> str:
    text = _DATE_WORDS.sub(" ", question)
    for _kind, pattern in _FILE_TYPES:
        text = pattern.sub(" ", text)
    text = _LOCAL_SCOPE.sub(" ", text)
    text = _FILLER.sub(" ", text)
    text = re.sub(r"[^\w\s&.-]", " ", text)
    return " ".join(word for word in text.split() if word not in {".", "-"}).strip(" .")


def _search_text(question: str) -> str:
    """The request for the file-query parser: type and date words stay (the
    parser reads them), verbs, pronouns and storage names go."""
    text = _LOCAL_SCOPE.sub(" ", question)
    text = re.sub(
        r"\b(please|find|show(\s+me)?|list|search(\s+for)?|locate|get|give\s+me|display|"
        r"where('?s|\s+is|\s+are)?|which|what|all|every|my|the|in|on|google\s+drive|drive|"
        r"them|these|those|related\s+to|about)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(re.sub(r"[?!]", " ", text).split())


def _keep(question: str) -> str | None:
    if re.search(r"\b(newest|latest|most\s+recent)\b", question, _FLAGS):
        return "newest"
    if re.search(r"\b(oldest|original|first)\b", question, _FLAGS):
        return "oldest"
    return None


def _folder_name(kind: str, question: str) -> str | None:
    if kind == "create_folder":
        match = _FOLDER_NAME.search(question)
        return match.group("name").strip() if match else None
    if kind in ("move", "organize"):
        match = _MOVE_DESTINATION.search(question)
        if match:
            name = match.group("name") or match.group("name2") or match.group("name3")
            name = name.strip() if name else None
            if name and not _THERE.fullmatch(name) and name.lower() not in {"it", "there"}:
                return name
    return None


def understand(question: str) -> Request:
    kind = next((kind for kind, pattern in _RULES if pattern.search(question)), "ask")
    name = _folder_name(kind, question)
    request = Request(
        kind=kind,
        scope=_scope(question),
        file_type=_file_type(question),
        query=_subject(question) if kind not in ("ask", "create_folder") else "",
        search_text=_search_text(question),
        refers_to_previous=bool(_REFERENCE.search(question)),
        name=name,
        destination_is_last_folder=kind == "move"
        and name is None
        and bool(_THERE.search(question)),
        keep=_keep(question) if kind == "clean_duplicates" else None,
        permanent=bool(re.search(r"\bpermanent(ly)?\b|for\s+good|forever", question, _FLAGS)),
    )
    if kind == "move" and name:
        # The destination name isn't part of what to move.
        request = replace(request, query=_subject(question.replace(name, " ")))
    return request
