from dataclasses import dataclass, field

from vault_shared.db.models import FileLifecycleState
from vault_shared.storage_intelligence.thresholds import (
    INACTIVE_FILE_DAYS_DEFAULT,
    OLD_FILE_DAYS_DEFAULT,
    RECENTLY_ACTIVE_WITHIN_DAYS,
)

# Bumped whenever the rule chain below changes meaning, so a re-run of the
# "Analyze Organization" job can tell a stale `FileLifecycle.scorer_version`
# apart from a current one (same idea as `ENTITY_INFERENCE_VERSION`).
LIFECYCLE_SCORER_VERSION = "v1"


@dataclass(frozen=True)
class FileLifecycleSignals:
    """Everything the scorer needs about one file, gathered by
    `LifecycleService` from already-computed `FileRelationship`/
    `DuplicateGroupMember`/`FileEntityLink`/`File` data — no AI call
    anywhere in or behind this dataclass, by construction."""

    is_duplicate_not_kept: bool
    has_newer_version: bool
    is_linked_to_active_entity: bool
    is_linked_to_any_entity: bool
    has_any_relationship: bool
    has_classification: bool
    has_extraction: bool
    modified_days_ago: float | None
    viewed_days_ago: float | None


@dataclass(frozen=True)
class LifecycleResult:
    state: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)


def score_lifecycle(signals: FileLifecycleSignals) -> LifecycleResult:
    """Deterministic rule chain — first match wins. Reuses Storage
    Intelligence's own named age thresholds (`OLD_FILE_DAYS_DEFAULT`,
    `INACTIVE_FILE_DAYS_DEFAULT`, `RECENTLY_ACTIVE_WITHIN_DAYS`) rather than
    inventing new ones, so "old"/"inactive" means the same thing here as it
    already does on the Storage Overview. Spec: "Never equate old =
    delete" — note that age alone, without a *combination* of signals,
    never reaches a *_CANDIDATE state below; it only ever reaches KEEP,
    REVIEW_REQUIRED, or UNKNOWN."""
    raw_signals = {
        "is_duplicate_not_kept": signals.is_duplicate_not_kept,
        "has_newer_version": signals.has_newer_version,
        "is_linked_to_active_entity": signals.is_linked_to_active_entity,
        "is_linked_to_any_entity": signals.is_linked_to_any_entity,
        "has_any_relationship": signals.has_any_relationship,
        "has_classification": signals.has_classification,
        "has_extraction": signals.has_extraction,
        "modified_days_ago": signals.modified_days_ago,
        "viewed_days_ago": signals.viewed_days_ago,
    }

    if signals.is_duplicate_not_kept:
        return LifecycleResult(
            state=FileLifecycleState.DUPLICATE_CANDIDATE,
            confidence=0.8,
            evidence=["This file is a non-kept member of a duplicate group."],
            signals=raw_signals,
        )

    if signals.has_newer_version:
        return LifecycleResult(
            state=FileLifecycleState.OBSOLETE_CANDIDATE,
            confidence=0.75,
            evidence=["A newer version of this file exists (sequential_version relationship)."],
            signals=raw_signals,
        )

    if signals.is_linked_to_active_entity:
        if (
            signals.modified_days_ago is not None
            and signals.modified_days_ago <= RECENTLY_ACTIVE_WITHIN_DAYS
        ):
            return LifecycleResult(
                state=FileLifecycleState.ACTIVE,
                confidence=0.8,
                evidence=["Linked to an active project/client/campaign and modified recently."],
                signals=raw_signals,
            )
        if (
            signals.modified_days_ago is not None
            and signals.modified_days_ago <= OLD_FILE_DAYS_DEFAULT
        ):
            return LifecycleResult(
                state=FileLifecycleState.REFERENCE,
                confidence=0.6,
                evidence=[
                    "Linked to an active project/client/campaign but not recently modified — "
                    "likely reference material rather than active work."
                ],
                signals=raw_signals,
            )
        return LifecycleResult(
            state=FileLifecycleState.ARCHIVE_CANDIDATE,
            confidence=0.6,
            evidence=[
                "Linked to an active project/client/campaign but stale by more than "
                f"{OLD_FILE_DAYS_DEFAULT} days."
            ],
            signals=raw_signals,
        )

    if (
        signals.modified_days_ago is not None
        and signals.viewed_days_ago is not None
        and signals.modified_days_ago >= OLD_FILE_DAYS_DEFAULT
        and signals.viewed_days_ago >= INACTIVE_FILE_DAYS_DEFAULT
        and not signals.has_any_relationship
        and not signals.is_linked_to_any_entity
    ):
        return LifecycleResult(
            state=FileLifecycleState.ARCHIVE_CANDIDATE,
            confidence=0.55,
            evidence=[
                f"Not modified in over {OLD_FILE_DAYS_DEFAULT} days, not viewed in over "
                f"{INACTIVE_FILE_DAYS_DEFAULT} days, and has no known relationships or "
                "project/client/campaign link."
            ],
            signals=raw_signals,
        )

    conflicting = (
        signals.viewed_days_ago is None
        and signals.modified_days_ago is not None
        and signals.modified_days_ago >= OLD_FILE_DAYS_DEFAULT
    ) or (
        signals.viewed_days_ago is not None
        and signals.modified_days_ago is not None
        and signals.viewed_days_ago <= RECENTLY_ACTIVE_WITHIN_DAYS
        and signals.modified_days_ago >= OLD_FILE_DAYS_DEFAULT
    )
    if conflicting:
        return LifecycleResult(
            state=FileLifecycleState.REVIEW_REQUIRED,
            confidence=0.4,
            evidence=[
                "Age and activity signals conflict or are incomplete — needs human review "
                "rather than an automated judgment."
            ],
            signals=raw_signals,
        )

    if (
        signals.modified_days_ago is None
        and not signals.has_classification
        and not signals.has_extraction
        and not signals.has_any_relationship
        and not signals.is_linked_to_any_entity
    ):
        return LifecycleResult(
            state=FileLifecycleState.UNKNOWN,
            confidence=0.0,
            evidence=["No signal is available for this file yet."],
            signals=raw_signals,
        )

    return LifecycleResult(
        state=FileLifecycleState.KEEP,
        confidence=0.5,
        evidence=["No stale, duplicate, or obsolete signal found."],
        signals=raw_signals,
    )
