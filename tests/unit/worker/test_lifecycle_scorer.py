from dataclasses import replace

import pytest

from vault_shared.db.models import FileLifecycleState
from worker.organization.lifecycle_scorer import FileLifecycleSignals, score_lifecycle

_BASE = FileLifecycleSignals(
    is_duplicate_not_kept=False,
    has_newer_version=False,
    is_linked_to_active_entity=False,
    is_linked_to_any_entity=False,
    has_any_relationship=True,
    has_classification=True,
    has_extraction=True,
    modified_days_ago=10.0,
    viewed_days_ago=5.0,
)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"is_duplicate_not_kept": True}, FileLifecycleState.DUPLICATE_CANDIDATE),
        ({"has_newer_version": True}, FileLifecycleState.OBSOLETE_CANDIDATE),
        (
            {"is_linked_to_active_entity": True, "is_linked_to_any_entity": True},
            FileLifecycleState.ACTIVE,
        ),
        (
            {
                "is_linked_to_active_entity": True,
                "is_linked_to_any_entity": True,
                "modified_days_ago": 250.0,
            },
            FileLifecycleState.REFERENCE,
        ),
        (
            {
                "is_linked_to_active_entity": True,
                "is_linked_to_any_entity": True,
                "modified_days_ago": 800.0,
            },
            FileLifecycleState.ARCHIVE_CANDIDATE,
        ),
        (
            {"has_any_relationship": False, "modified_days_ago": 800.0, "viewed_days_ago": 800.0},
            FileLifecycleState.ARCHIVE_CANDIDATE,
        ),
        ({"modified_days_ago": 800.0, "viewed_days_ago": None}, FileLifecycleState.REVIEW_REQUIRED),
        ({"modified_days_ago": 800.0, "viewed_days_ago": 3.0}, FileLifecycleState.REVIEW_REQUIRED),
        (
            {
                "has_any_relationship": False,
                "has_classification": False,
                "has_extraction": False,
                "modified_days_ago": None,
                "viewed_days_ago": None,
            },
            FileLifecycleState.UNKNOWN,
        ),
        ({}, FileLifecycleState.KEEP),
    ],
)
def test_every_lifecycle_state_is_reachable(overrides: dict, expected: str) -> None:
    assert score_lifecycle(replace(_BASE, **overrides)).state == expected


def test_age_alone_never_makes_a_related_file_an_archive_candidate() -> None:
    """Spec: "Never equate old = delete" — an old file that still has a
    relationship is not proposed for archiving on age alone."""
    result = score_lifecycle(replace(_BASE, modified_days_ago=900.0, viewed_days_ago=900.0))

    assert result.state == FileLifecycleState.KEEP


def test_every_result_explains_itself_with_evidence() -> None:
    result = score_lifecycle(replace(_BASE, is_duplicate_not_kept=True))

    assert result.evidence
    assert result.signals["is_duplicate_not_kept"] is True
