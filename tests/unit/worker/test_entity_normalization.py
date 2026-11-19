import pytest

from worker.intelligence.entity_inference_service import normalize_entity_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Corp.", "acme corp"),
        ("  ACME   corp ", "acme corp"),
        ("Acme-Corp", "acme corp"),
        ("Phoenix", "phoenix"),
    ],
)
def test_variants_of_one_name_normalize_to_the_same_key(raw: str, expected: str) -> None:
    assert normalize_entity_name(raw) == expected


def test_distinct_names_stay_distinct() -> None:
    assert normalize_entity_name("Phoenix") != normalize_entity_name("Project Phoenix")
