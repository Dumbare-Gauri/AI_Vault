"""Deterministic rename proposals. The words in a proposed name come only
from evidence AI Vault already holds about the file — its linked
project/client/campaign and its AI-read document type — never from a fresh
model call, so a proposal can always be explained and never invents a name."""

import re
from datetime import date
from pathlib import PurePosixPath

_MAX_NAME_LENGTH = 120
_UNINFORMATIVE_STEM = re.compile(
    r"^(untitled( (document|spreadsheet|presentation|form|drawing))?"
    r"|new( text)? document|new file"
    r"|document ?\d*|doc ?\d*|file ?\d*|scan ?\d*|img[ _-]?\d+|image ?\d*|photo ?\d*"
    r"|screenshot.*|notes?|draft|final( final)*|temp|test|asdf+|\d+)$"
)
_COPY_PREFIX = re.compile(r"^(copy of )+")
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)$")
_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_NON_EVIDENCE_TYPES = {"", "unknown", "other", "none", "n/a"}


def _split(name: str) -> tuple[str, str]:
    suffix = PurePosixPath(name).suffix
    if not suffix or len(suffix) > 6 or " " in suffix:
        return name, ""
    return name[: -len(suffix)], suffix


def is_uninformative_name(name: str) -> bool:
    stem, _ = _split(name.strip())
    stem = _DUPLICATE_SUFFIX.sub("", _COPY_PREFIX.sub("", stem.lower().strip()))
    return bool(_UNINFORMATIVE_STEM.match(stem.strip()))


def _clean(part: str) -> str:
    return " ".join(_UNSAFE.sub(" ", part).split())


def propose_name(
    *,
    current_name: str,
    entity_name: str | None,
    document_type: str | None,
    dated: date | None,
) -> str | None:
    parts: list[str] = []
    if entity_name and _clean(entity_name):
        parts.append(_clean(entity_name))
    if document_type and document_type.strip().lower() not in _NON_EVIDENCE_TYPES:
        parts.append(_clean(document_type).title())
    if not parts:
        return None
    if dated is not None:
        parts.append(dated.strftime("%Y-%m"))

    _, extension = _split(current_name)
    stem = " - ".join(parts)[: _MAX_NAME_LENGTH - len(extension)].rstrip(" -")
    proposed = f"{stem}{extension}"
    return None if proposed == current_name else proposed
