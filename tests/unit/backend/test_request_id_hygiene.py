import uuid

import pytest

from app.core.logging_middleware import resolve_request_id


def test_a_well_formed_request_id_is_kept() -> None:
    assert resolve_request_id("req-abc.123_X") == "req-abc.123_X"


@pytest.mark.parametrize(
    "hostile",
    [
        "line\nbreak",
        'quote"injection',
        '{"level":"critical"}',
        "a" * 65,
        "",
        "has space",
        "<script>",
        "../../etc/passwd",
    ],
)
def test_a_hostile_request_id_is_replaced_with_a_generated_one(hostile: str) -> None:
    resolved = resolve_request_id(hostile)

    assert resolved != hostile
    uuid.UUID(resolved)


def test_a_missing_request_id_is_generated() -> None:
    uuid.UUID(resolve_request_id(None))
