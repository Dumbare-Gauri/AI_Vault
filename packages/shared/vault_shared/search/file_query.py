"""Structured file search — the deterministic half of "search must work".

`parse_file_query` turns everyday phrasing ("python files larger than 500 MB
modified before 2023") into a `FileQuery` with no AI involved. Only what it
cannot place is left as free `text`, matched against names, paths and
discovered projects/clients. The AI layer may *propose* a `FileQuery` for
phrasing this parser misses, but results always come from running the query
against the real index — never from the model.
"""

import re
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta

# category -> (file extensions, mime-type prefixes)
FILE_CATEGORIES: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "python": (frozenset({"py", "ipynb", "pyw", "pyi"}), ()),
    "code": (
        frozenset(
            {
                "py",
                "ipynb",
                "js",
                "jsx",
                "ts",
                "tsx",
                "java",
                "go",
                "rs",
                "rb",
                "php",
                "c",
                "cc",
                "cpp",
                "h",
                "hpp",
                "cs",
                "swift",
                "kt",
                "scala",
                "sql",
                "sh",
                "ps1",
                "html",
                "css",
                "scss",
                "vue",
                "json",
                "yaml",
                "yml",
                "toml",
                "r",
                "m",
                "dart",
            }
        ),
        (),
    ),
    "image": (
        frozenset({"png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "tif", "tiff", "svg"}),
        ("image/",),
    ),
    "video": (frozenset({"mp4", "mov", "avi", "mkv", "webm", "m4v"}), ("video/",)),
    "audio": (frozenset({"mp3", "wav", "m4a", "aac", "flac", "ogg"}), ("audio/",)),
    "pdf": (frozenset({"pdf"}), ("application/pdf",)),
    "spreadsheet": (
        frozenset({"xlsx", "xls", "csv", "ods", "tsv"}),
        ("application/vnd.google-apps.spreadsheet",),
    ),
    "document": (
        frozenset({"doc", "docx", "txt", "md", "rtf", "odt", "pages"}),
        ("application/vnd.google-apps.document",),
    ),
    "presentation": (
        frozenset({"ppt", "pptx", "key", "odp"}),
        ("application/vnd.google-apps.presentation",),
    ),
    "archive": (frozenset({"zip", "rar", "7z", "tar", "gz", "tgz", "bz2"}), ()),
    "design": (frozenset({"fig", "psd", "ai", "sketch", "xd", "indd", "eps", "svg"}), ()),
}

_CATEGORY_WORDS: dict[str, str] = {
    "python": "python",
    "py": "python",
    "notebook": "python",
    "notebooks": "python",
    "code": "code",
    "source": "code",
    "script": "code",
    "scripts": "code",
    "image": "image",
    "images": "image",
    "photo": "image",
    "photos": "image",
    "picture": "image",
    "pictures": "image",
    "png": "image",
    "jpg": "image",
    "screenshot": "image",
    "screenshots": "image",
    "video": "video",
    "videos": "video",
    "audio": "audio",
    "music": "audio",
    "pdf": "pdf",
    "pdfs": "pdf",
    "spreadsheet": "spreadsheet",
    "spreadsheets": "spreadsheet",
    "excel": "spreadsheet",
    "sheet": "spreadsheet",
    "sheets": "spreadsheet",
    "csv": "spreadsheet",
    "document": "document",
    "documents": "document",
    "doc": "document",
    "docs": "document",
    "word": "document",
    "presentation": "presentation",
    "presentations": "presentation",
    "slides": "presentation",
    "deck": "presentation",
    "decks": "presentation",
    "powerpoint": "presentation",
    "zip": "archive",
    "zips": "archive",
    "archives": "archive",
    "creative": "design",
    "creatives": "design",
    "design": "design",
    "designs": "design",
    "figma": "design",
    "photoshop": "design",
}

_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
_SIZE_RE = re.compile(
    r"\b(?P<op>larger than|bigger than|greater than|more than|over|above|at least|>=?|"
    r"smaller than|less than|under|below|at most|<=?)\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>tb|gb|mb|kb|b)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(
    r"\b(?P<op>before|after|since|in|during|from)\s+(?P<year>(?:19|20)\d{2})"
    r"(?:-(?P<month>\d{1,2}))?\b",
    re.IGNORECASE,
)
_AGE_RE = re.compile(
    r"\b(?:older than|not (?:modified|touched|opened) (?:in|for))\s+(?P<num>\d+)\s*"
    r"(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_RECENT_RE = re.compile(
    r"\b(?:in the |from the )?(?:last|past)\s+(?:(?P<num>\d+)\s+)?"
    r"(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_FOLDER_RE = re.compile(
    r"\bin (?:the )?(?:folder\s+[\"'“]?(?P<a>[^\"'”]+?)[\"'”]?|"
    r"[\"'“](?P<b>[^\"'”]+)[\"'”] folder|(?P<c>\w[\w&-]*) folder)(?=$|\s)",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"“](?P<q>[^\"”]+)[\"”]")
_EXT_RE = re.compile(r"(?<![\w])\.(?P<ext>[a-z0-9]{1,8})\b", re.IGNORECASE)
_LARGE_RE = re.compile(r"\b(?:large|largest|big|biggest|huge)\b", re.IGNORECASE)
_OLD_RE = re.compile(r"\b(?:old|oldest|stale|outdated)\b", re.IGNORECASE)
_SHARED_RE = re.compile(r"\bshared with me\b", re.IGNORECASE)
_MINE_RE = re.compile(r"\b(?:my own|owned by me|that i own)\b", re.IGNORECASE)

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "all", "any", "every", "my", "our", "me", "i", "we", "us", "show",
        "find", "search", "list", "get", "give", "look", "looking", "for", "file", "files",
        "item", "items", "stuff", "thing", "things", "please", "can", "you", "could", "would",
        "which", "what", "that", "are", "is", "was", "were", "be", "been", "to", "of", "in",
        "on", "at", "by", "with", "from", "and", "or", "related", "relating", "about",
        "regarding", "containing", "contain", "contains", "named", "called", "modified",
        "created", "updated", "edited", "changed", "than", "there", "here", "some", "these",
        "those",
    }
)  # fmt: skip

_DEFAULT_OLD_DAYS = 365
_LARGE_FILE_BYTES = 100 * 1024**2


@dataclass(frozen=True)
class FileQuery:
    text: str = ""
    categories: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    size_min: int | None = None
    size_max: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    folder: str | None = None
    connector_id: str | None = None
    ownership: str | None = None  # "mine" | "shared" | None
    sort: str = "relevance"  # relevance | largest | newest | oldest
    limit: int = 50
    offset: int = 0
    # Human-readable list of what was understood, shown back to the user.
    understood: tuple[str, ...] = field(default=(), compare=False)

    @property
    def has_filters(self) -> bool:
        return any(
            (
                self.categories,
                self.extensions,
                self.size_min is not None,
                self.size_max is not None,
                self.modified_after is not None,
                self.modified_before is not None,
                self.folder,
                self.connector_id,
                self.ownership,
            )
        )


def _days(num: int, unit: str) -> int:
    unit = unit.lower().rstrip("s")
    return num * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]


def _human_bytes(value: int) -> str:
    for unit, size in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if value >= size:
            return f"{value / size:g} {unit}"
    return f"{value} B"


def parse_file_query(
    raw: str, *, now: datetime | None = None, detect_categories: bool = True
) -> FileQuery:
    now = now or datetime.now(UTC)
    remaining = f" {raw.strip()} "
    understood: list[str] = []
    values: dict = {}

    def consume(match: re.Match[str]) -> None:
        nonlocal remaining
        remaining = remaining.replace(match.group(0), " ", 1)

    for m in list(_FOLDER_RE.finditer(remaining)):
        folder = (m.group("a") or m.group("b") or m.group("c") or "").strip()
        if folder:
            values["folder"] = folder
            understood.append(f"in folder “{folder}”")
            consume(m)

    quoted = [m.group("q").strip() for m in _QUOTED_RE.finditer(remaining)]
    for m in list(_QUOTED_RE.finditer(remaining)):
        consume(m)

    for m in list(_SIZE_RE.finditer(remaining)):
        amount = int(float(m.group("num")) * _UNITS[m.group("unit").lower()])
        op = m.group("op").lower()
        if op.startswith(("larger", "bigger", "greater", "more", "over", "above", "at least", ">")):
            values["size_min"] = amount
            understood.append(f"larger than {_human_bytes(amount)}")
        else:
            values["size_max"] = amount
            understood.append(f"smaller than {_human_bytes(amount)}")
        consume(m)

    for m in list(_YEAR_RE.finditer(remaining)):
        year = int(m.group("year"))
        month = int(m.group("month")) if m.group("month") else None
        start = datetime(year, month or 1, 1, tzinfo=UTC)
        end = (
            datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
            if month
            else datetime(year + 1, 1, 1, tzinfo=UTC)
        )
        label = f"{year}-{month:02d}" if month else str(year)
        op = m.group("op").lower()
        if op == "before":
            values["modified_before"] = start
            understood.append(f"modified before {label}")
        elif op in ("after", "since"):
            values["modified_after"] = start if op == "since" else end
            understood.append(f"modified {op} {label}")
        else:
            values["modified_after"], values["modified_before"] = start, end
            understood.append(f"modified in {label}")
        consume(m)

    for m in list(_AGE_RE.finditer(remaining)):
        days = _days(int(m.group("num")), m.group("unit"))
        values["modified_before"] = now - timedelta(days=days)
        understood.append(f"not modified in {m.group('num')} {m.group('unit')}")
        consume(m)

    for m in list(_RECENT_RE.finditer(remaining)):
        days = _days(int(m.group("num") or 1), m.group("unit"))
        values["modified_after"] = now - timedelta(days=days)
        window = f"{m.group('num')} {m.group('unit')}" if m.group("num") else m.group("unit")
        understood.append(f"modified in the last {window}")
        consume(m)

    if _SHARED_RE.search(remaining):
        values["ownership"] = "shared"
        understood.append("shared with me")
        remaining = _SHARED_RE.sub(" ", remaining)
    elif _MINE_RE.search(remaining):
        values["ownership"] = "mine"
        understood.append("owned by me")
        remaining = _MINE_RE.sub(" ", remaining)

    extensions = [m.group("ext").lower() for m in _EXT_RE.finditer(remaining)]
    remaining = _EXT_RE.sub(" ", remaining)

    if _LARGE_RE.search(remaining) and "size_min" not in values:
        values["size_min"] = _LARGE_FILE_BYTES
        values["sort"] = "largest"
        understood.append(f"larger than {_human_bytes(_LARGE_FILE_BYTES)}")
        remaining = _LARGE_RE.sub(" ", remaining)
    if _OLD_RE.search(remaining) and "modified_before" not in values:
        values["modified_before"] = now - timedelta(days=_DEFAULT_OLD_DAYS)
        understood.append("not modified in over a year")
        remaining = _OLD_RE.sub(" ", remaining)

    categories: list[str] = []
    text_tokens: list[str] = []
    for token in re.findall(r"[\w&'-]+", remaining):
        lowered = token.lower()
        category = _CATEGORY_WORDS.get(lowered) if detect_categories else None
        if category is not None:
            if category not in categories:
                categories.append(category)
            continue
        if lowered in _STOPWORDS:
            continue
        text_tokens.append(token)

    for category in categories:
        understood.append(f"{category} files")
    for ext in extensions:
        understood.append(f".{ext} files")
    text = " ".join([*quoted, *text_tokens]).strip()
    if text:
        understood.append(f"matching “{text}”")

    if values.get("size_min") is not None and "sort" not in values:
        values["sort"] = "largest"
    return FileQuery(
        text=text,
        categories=tuple(categories),
        extensions=tuple(dict.fromkeys(extensions)),
        understood=tuple(understood),
        **values,
    )


def merge_queries(base: FileQuery, override: FileQuery) -> FileQuery:
    """Explicit UI filters (`override`) win over what was parsed from text."""
    changes = {
        f.name: getattr(override, f.name)
        for f in fields(FileQuery)
        if f.name not in ("understood", "limit", "offset", "sort", "text")
        and getattr(override, f.name) not in (None, (), "")
    }
    if override.sort != "relevance":
        changes["sort"] = override.sort
    return replace(base, **changes, limit=override.limit, offset=override.offset)


def describe_query(query: FileQuery) -> list[str]:
    return list(query.understood)
