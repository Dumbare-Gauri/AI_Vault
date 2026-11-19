import pytest

from vault_shared import ValidationError
from vault_shared.archive_naming import clean_archive_name, numbered_archive_name


def test_a_plain_name_gets_the_zip_extension() -> None:
    assert clean_archive_name("  Project_Docs_Archive ") == "Project_Docs_Archive.zip"


def test_an_existing_zip_extension_is_not_doubled() -> None:
    assert clean_archive_name("Backup.ZIP") == "Backup.zip"


@pytest.mark.parametrize(
    "name", ["", "   ", ".zip", "../escape", "a/b", "C:\\x", "what?", "con", "trailing."]
)
def test_unusable_names_are_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        clean_archive_name(name)


def test_an_overlong_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        clean_archive_name("x" * 300)


def test_a_free_name_is_kept() -> None:
    assert numbered_archive_name("Docs.zip", {"Other.zip"}) == "Docs.zip"


def test_a_taken_name_gets_the_next_free_number() -> None:
    taken = {"docs.zip", "Docs (2).zip"}

    assert numbered_archive_name("Docs.zip", taken) == "Docs (3).zip"
