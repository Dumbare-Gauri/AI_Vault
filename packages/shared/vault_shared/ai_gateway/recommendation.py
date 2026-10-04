"""The AI output boundary.

A model response is an UNTRUSTED RECOMMENDATION, never a command. This module
turns raw model text into a `ValidatedRecommendation` — and "validated" means
only "structurally safe to show and to feed to the policy engine". It does not
mean authorized: confidence never replaces permissions, policy, or human
confirmation, and `requires_confirmation` is not read from the model at all.

What the model may reference: only the short `ref` handles AI Vault supplied
in the `AIContext`. It cannot introduce database IDs, provider IDs, paths,
URLs, SQL, or shell commands — any structured field that tries is rejected,
and anything unknown to this schema is discarded rather than passed on.
"""

import enum
import json
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_ACTIONS = 50
MAX_FOLDER_DEPTH = 6
MAX_NAME_LENGTH = 255
MAX_REASON_LENGTH = 500
MAX_SUMMARY_LENGTH = 1000
MAX_EVIDENCE_ITEMS = 5
MAX_EVIDENCE_LENGTH = 300
MAX_LABEL_LENGTH = 120

ALLOWED_LABEL_KEYS = frozenset(
    {"client", "project", "campaign", "asset_type", "status", "purpose", "content_category"}
)


class RecommendationKind(enum.StrEnum):
    """What the model may *propose*. Deliberately excludes trash, delete,
    permanent delete, restore, upload, download, permission and
    authentication changes: no reasoning output can request those."""

    CLASSIFY = "classify"
    RENAME = "rename"
    MOVE = "move"
    CREATE_FOLDER = "create_folder"
    ARCHIVE = "archive"
    MARK_DUPLICATE = "mark_duplicate"
    NEEDS_REVIEW = "needs_review"


_NEEDS_TARGET = frozenset(RecommendationKind) - {RecommendationKind.CREATE_FOLDER}

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]{1,15}://|\bwww\.|\bdata:[a-z]+/", re.IGNORECASE)
_SHELL_RE = re.compile(
    r"\$\(|`|&&|\|\s*(?:sh|bash|zsh)\b|;\s*(?:rm|curl|wget|nc|chmod|sudo)\b|\brm\s+-[rf]",
    re.IGNORECASE,
)
_SQL_RE = re.compile(
    r"\bdrop\s+table\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|"
    r"\bdelete\s+from\s+\w+\s+where\b|\bunion\s+select\b|"
    r";\s*(?:drop|delete|insert|update|alter|truncate)\b",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _has_hidden_characters(text: str) -> bool:
    """Control, zero-width and bidi-override characters can disguise a name
    (`invoice‮gpj.exe` displays as `invoiceexe.jpg`)."""
    return any(unicodedata.category(ch) in _HIDDEN_CATEGORIES for ch in text)


class AIResponseRejected(Exception):
    """The whole response was unusable (not JSON, not an object, wrong
    shape). Individual bad actions do not raise — they are dropped and
    reported in `ValidatedRecommendation.rejected`."""


@dataclass(frozen=True)
class RejectedItem:
    index: int
    reason: str


@dataclass(frozen=True)
class ValidatedAction:
    kind: RecommendationKind
    target_ref: str | None
    reason: str
    confidence: float
    proposed_name: str | None = None
    destination_folder: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedRecommendation:
    intent: str
    actions: tuple[ValidatedAction, ...]
    rejected: tuple[RejectedItem, ...]
    reasoning_summary: str
    confidence: float | None
    evidence: tuple[str, ...]
    unknowns: tuple[str, ...]
    # Fixed by AI Vault, never taken from the model. A later policy engine
    # may relax this for genuinely low-risk kinds; the model cannot.
    requires_confirmation: bool = True
    authoritative: bool = False


def find_disallowed_content(text: str) -> str | None:
    """Strict check for *structured* fields (names, folder components,
    labels): a URL, shell construct, or SQL fragment there is an attempt to
    smuggle something executable, not a description."""
    if _URL_RE.search(text):
        return "contains a URL"
    if _SHELL_RE.search(text):
        return "contains a shell construct"
    if _SQL_RE.search(text):
        return "contains an SQL fragment"
    return None


def neutralize_free_text(text: str, *, max_length: int) -> str:
    """For descriptive fields (reason, evidence, summary): shown to a
    human, never interpreted. Links and executable-looking fragments are
    replaced rather than rejecting the whole action."""
    cleaned = _CONTROL_RE.sub(" ", text)
    cleaned = "".join(ch for ch in cleaned if unicodedata.category(ch) not in _HIDDEN_CATEGORIES)
    cleaned = _URL_RE.sub("[link removed]", cleaned)
    cleaned = _SHELL_RE.sub("[removed]", cleaned)
    cleaned = _SQL_RE.sub("[removed]", cleaned)
    return " ".join(cleaned.split())[:max_length]


def validate_component_name(value: object, *, what: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """A single name (file or folder component), never a path."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{what} is too long")
    if any(sep in value for sep in ("/", "\\")):
        raise ValueError(f"{what} must not contain path separators")
    if value.strip() in {".", ".."}:
        raise ValueError(f"{what} must not be '.' or '..'")
    if _has_hidden_characters(value):
        raise ValueError(f"{what} must not contain control or invisible characters")
    if value != value.strip() or value.endswith("."):
        raise ValueError(f"{what} must not have leading/trailing spaces or a trailing dot")
    problem = find_disallowed_content(value)
    if problem:
        raise ValueError(f"{what} {problem}")
    return value


def _validate_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("confidence must be a number")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return float(value)


def _parse_json_object(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    text = _FENCE_RE.sub("", raw.strip())
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AIResponseRejected("response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AIResponseRejected("response is not a JSON object")
    return payload


def _validate_action(raw: object, allowed_refs: Collection[str]) -> ValidatedAction:
    if not isinstance(raw, dict):
        raise ValueError("action is not an object")

    raw_kind = raw.get("kind")
    try:
        kind = RecommendationKind(raw_kind if isinstance(raw_kind, str) else "")
    except ValueError as exc:
        raise ValueError(f"unsupported action kind {str(raw_kind)[:40]!r}") from exc

    target_ref = raw.get("target_ref")
    if kind in _NEEDS_TARGET:
        if not isinstance(target_ref, str) or target_ref not in allowed_refs:
            raise ValueError("target_ref is not one of the supplied candidate refs")
    else:
        known = isinstance(target_ref, str) and target_ref in allowed_refs
        target_ref = target_ref if known else None

    proposed_name: str | None = None
    if kind is RecommendationKind.RENAME:
        proposed_name = validate_component_name(raw.get("proposed_name"), what="proposed_name")

    destination: tuple[str, ...] = ()
    if kind in {RecommendationKind.MOVE, RecommendationKind.CREATE_FOLDER}:
        parts = raw.get("destination_folder")
        if not isinstance(parts, list) or not parts or len(parts) > MAX_FOLDER_DEPTH:
            raise ValueError("destination_folder must be a short list of folder names")
        destination = tuple(validate_component_name(p, what="destination_folder") for p in parts)

    labels: dict[str, str] = {}
    if kind is RecommendationKind.CLASSIFY:
        raw_labels = raw.get("labels")
        if not isinstance(raw_labels, dict) or not raw_labels:
            raise ValueError("classify requires labels")
        for key, value in raw_labels.items():
            if key not in ALLOWED_LABEL_KEYS:
                continue
            labels[key] = validate_component_name(
                value, what=f"label {key}", max_length=MAX_LABEL_LENGTH
            )
        if not labels:
            raise ValueError("classify has no recognized labels")

    evidence_raw = raw.get("evidence", [])
    evidence = tuple(
        neutralize_free_text(item, max_length=MAX_EVIDENCE_LENGTH)
        for item in (evidence_raw if isinstance(evidence_raw, list) else [])[:MAX_EVIDENCE_ITEMS]
        if isinstance(item, str)
    )

    reason = raw.get("reason")
    return ValidatedAction(
        kind=kind,
        target_ref=target_ref,
        reason=neutralize_free_text(reason, max_length=MAX_REASON_LENGTH)
        if isinstance(reason, str)
        else "",
        confidence=_validate_confidence(raw.get("confidence")),
        proposed_name=proposed_name,
        destination_folder=destination,
        labels=labels,
        evidence=evidence,
    )


def _strings(value: object, *, limit: int, length: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        neutralize_free_text(item, max_length=length)
        for item in value[:limit]
        if isinstance(item, str)
    )


def validate_recommendation(
    raw: str | Mapping[str, Any], *, allowed_refs: Collection[str]
) -> ValidatedRecommendation:
    """Raises `AIResponseRejected` only when nothing is usable. Otherwise
    valid actions are returned and each invalid one is dropped with a
    reason — a single bad action never blocks the rest, and never passes
    through."""
    payload = _parse_json_object(raw)

    raw_actions = payload.get("recommendations", [])
    if not isinstance(raw_actions, list):
        raise AIResponseRejected("recommendations is not a list")

    actions: list[ValidatedAction] = []
    rejected: list[RejectedItem] = []
    for index, raw_action in enumerate(raw_actions[:MAX_ACTIONS]):
        try:
            actions.append(_validate_action(raw_action, allowed_refs))
        except ValueError as exc:
            rejected.append(RejectedItem(index=index, reason=str(exc)))
    for index in range(MAX_ACTIONS, len(raw_actions)):
        rejected.append(RejectedItem(index=index, reason="too many recommendations"))

    try:
        confidence: float | None = _validate_confidence(payload.get("confidence"))
    except ValueError:
        confidence = None

    intent = payload.get("intent")
    summary = payload.get("reasoning_summary")
    return ValidatedRecommendation(
        intent=neutralize_free_text(intent, max_length=200) if isinstance(intent, str) else "",
        actions=tuple(actions),
        rejected=tuple(rejected),
        reasoning_summary=neutralize_free_text(summary, max_length=MAX_SUMMARY_LENGTH)
        if isinstance(summary, str)
        else "",
        confidence=confidence,
        evidence=_strings(
            payload.get("evidence"), limit=MAX_EVIDENCE_ITEMS, length=MAX_EVIDENCE_LENGTH
        ),
        unknowns=_strings(payload.get("unknowns"), limit=10, length=MAX_EVIDENCE_LENGTH),
    )


RECOMMENDATION_OUTPUT_INSTRUCTIONS = """
Respond with ONLY one JSON object, no markdown, in exactly this shape:
{
  "intent": string,
  "recommendations": [
    {"kind": "classify|rename|move|create_folder|archive|mark_duplicate|needs_review",
     "target_ref": string (a ref from the untrusted_data blocks; omit for create_folder),
     "proposed_name": string (rename only; a single file name, no slashes),
     "destination_folder": [string, ...] (move/create_folder only; folder names, not a path),
     "labels": {"client"|"project"|"campaign"|"asset_type"|"status"|"purpose"|\
"content_category": string} (classify only),
     "reason": string, "confidence": number 0-1, "evidence": [string, ...]}
  ],
  "reasoning_summary": string,
  "confidence": number 0-1,
  "evidence": [string, ...],
  "unknowns": [string, ...]
}
If you cannot determine something, put it in "unknowns" or use "needs_review". Never guess a \
client, project, or name you cannot support with evidence from the provided text.
""".strip()
