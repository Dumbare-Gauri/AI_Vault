import pytest

from app.application.vault_ai.understanding import understand


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("How many files do I have?", "count_files"),
        ("What is my largest file?", "largest_files"),
        ("Which files are using the most storage?", "largest_files"),
        ("What is consuming the most storage?", "breakdown"),
        ("Find all PDFs.", "find_files"),
        ("Find all Blarrow files", "find_files"),
        ("Where is the invoice?", "find_files"),
        ("Show me all files from last year", "find_files"),
        ("Show me every duplicate", "duplicates"),
        ("Show all duplicate videos", "duplicates"),
        ("List all duplicate files in my local storage", "duplicates"),
        ("Delete duplicate files", "clean_duplicates"),
        ("Keep the newest version", "clean_duplicates"),
        ("How much storage can I recover?", "storage_summary"),
        ("Which files can I safely archive?", "archive_candidates"),
        ("Create a folder called Blarrow 2026", "create_folder"),
        ("Move these files there", "move"),
        ("Move them into a new folder called Finance", "move"),
        ("Archive these old files", "archive"),
        ("Organize my Blarrow files", "organize"),
        ("Rename these files properly", "rename"),
        ("What did you change?", "what_changed"),
        ("What is the payment deadline in the contract?", "ask"),
        ("Who is the client in this proposal?", "ask"),
        ("Trash them", "trash"),
        ("Restore those files", "restore"),
    ],
)
def test_everyday_requests_are_understood(question: str, kind: str) -> None:
    assert understand(question).kind == kind


def test_local_storage_is_the_computer() -> None:
    assert understand("List all duplicate files in my local storage").scope == "local"


def test_google_drive_is_the_drive_scope() -> None:
    assert understand("find PDFs in my Google Drive").scope == "drive"


def test_a_hard_drive_is_local_not_google() -> None:
    assert understand("what is on my hard drive").scope == "local"


def test_no_storage_named_means_everywhere() -> None:
    assert understand("show duplicates").scope is None


def test_a_file_type_is_picked_up() -> None:
    assert understand("Show all duplicate videos").file_type == "video"


def test_the_subject_of_a_search_is_kept() -> None:
    assert understand("Find all Blarrow files").query == "Blarrow"


def test_pronouns_refer_to_the_previous_result() -> None:
    assert understand("Move them into a new folder called Finance").refers_to_previous


def test_the_destination_folder_name_is_extracted() -> None:
    assert understand("Move them into a new folder called Finance").name == "Finance"


def test_there_means_the_folder_just_made() -> None:
    request = understand("Move these files there")
    assert request.name is None and request.destination_is_last_folder


def test_the_new_folder_name_is_extracted() -> None:
    assert understand("Create a folder called Blarrow 2026.").name == "Blarrow 2026"


def test_keeping_the_newest_copy_is_understood() -> None:
    assert understand("Keep the newest version").keep == "newest"


def test_permanent_deletion_is_flagged() -> None:
    assert understand("permanently delete them").permanent


def test_organize_targets_a_subject() -> None:
    assert understand("Organize my Blarrow files").query == "Blarrow"


def test_the_ones_on_my_computer_refers_to_the_previous_result_on_that_storage() -> None:
    request = understand("move the ones on my computer into Docs Review")
    assert request.refers_to_previous
    assert request.scope == "local"
    assert request.name == "Docs Review"
