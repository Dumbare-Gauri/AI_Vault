from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from worker.storage_intelligence.duplicate_detector import _recommend_keep

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _file(
    name: str,
    *,
    created: datetime | None,
    modified: datetime | None = None,
    path: str | None = None,
):
    return SimpleNamespace(
        id=name,
        name=name,
        path=path or f"/{name}",
        provider_created_at=created,
        provider_modified_at=modified,
        size_bytes=1000,
    )


@pytest.mark.parametrize("reverse_input_order", [False, True])
def test_the_earliest_created_copy_is_kept_when_every_signal_ties(
    reverse_input_order: bool,
) -> None:
    original = _file("report.pdf", created=_NOW)
    copy = _file("report backup.pdf", created=_NOW + timedelta(seconds=5))
    members = [original, copy]
    if reverse_input_order:
        members.reverse()

    keep, _reason, _confidence = _recommend_keep(members)

    assert keep is original


@pytest.mark.parametrize("reverse_input_order", [False, True])
def test_identical_timestamps_fall_back_to_the_name_so_the_result_never_depends_on_row_order(
    reverse_input_order: bool,
) -> None:
    first = _file("a.pdf", created=_NOW)
    second = _file("b.pdf", created=_NOW)
    members = [first, second]
    if reverse_input_order:
        members.reverse()

    keep, _reason, _confidence = _recommend_keep(members)

    assert keep is first


def test_the_tiebreak_never_overrides_a_real_signal() -> None:
    older = _file("old.pdf", created=_NOW, modified=_NOW)
    newer = _file("new.pdf", created=_NOW + timedelta(days=1), modified=_NOW + timedelta(days=30))

    keep, _reason, _confidence = _recommend_keep([older, newer])

    assert keep is newer


def test_a_structured_location_still_beats_the_tiebreak() -> None:
    downloads = _file("a.pdf", created=_NOW, path="/Downloads/a.pdf")
    organized = _file("b.pdf", created=_NOW + timedelta(days=1), path="/Clients/Nike/b.pdf")

    keep, _reason, _confidence = _recommend_keep([downloads, organized])

    assert keep is organized


def test_a_missing_created_time_sorts_last_and_does_not_crash() -> None:
    dated = _file("dated.pdf", created=_NOW)
    undated = _file("undated.pdf", created=None)

    keep, _reason, _confidence = _recommend_keep([undated, dated])

    assert keep is dated
