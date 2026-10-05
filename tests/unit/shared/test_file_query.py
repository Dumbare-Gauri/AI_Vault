from datetime import UTC, datetime, timedelta

from vault_shared.search import FileQuery, merge_queries, parse_file_query

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def parse(text: str) -> FileQuery:
    return parse_file_query(text, now=NOW)


def test_python_files_become_a_category_not_text() -> None:
    query = parse("Find all Python files")
    assert query.categories == ("python",)
    assert query.text == ""


def test_size_threshold_is_parsed_from_larger_than() -> None:
    query = parse("Find files larger than 500 MB")
    assert query.size_min == 500 * 1024**2
    assert query.sort == "largest"
    assert query.text == ""


def test_an_absolute_year_bound_is_parsed() -> None:
    query = parse("Find files modified before 2023")
    assert query.modified_before == datetime(2023, 1, 1, tzinfo=UTC)
    assert query.text == ""


def test_in_a_year_is_a_range() -> None:
    query = parse("presentations in 2024")
    assert query.categories == ("presentation",)
    assert query.modified_after == datetime(2024, 1, 1, tzinfo=UTC)
    assert query.modified_before == datetime(2025, 1, 1, tzinfo=UTC)


def test_a_name_is_left_as_text() -> None:
    query = parse("Find Blarrow files")
    assert query.text == "Blarrow"
    assert not query.has_filters


def test_old_creative_files_for_a_client() -> None:
    query = parse("Find all old Blarrow creative files")
    assert query.text == "Blarrow"
    assert query.categories == ("design",)
    assert query.modified_before == NOW - timedelta(days=365)


def test_images_are_a_category() -> None:
    assert parse("Find all images").categories == ("image",)


def test_relative_age_and_recent_windows() -> None:
    assert parse("files older than 6 months").modified_before == NOW - timedelta(days=180)
    assert parse("pdfs from the last 2 weeks").modified_after == NOW - timedelta(days=14)


def test_folder_and_extension_and_sharing() -> None:
    query = parse('.fig files in the "Website Redesign" folder shared with me')
    assert query.extensions == ("fig",)
    assert query.folder == "Website Redesign"
    assert query.ownership == "shared"


def test_files_related_to_a_project() -> None:
    assert parse("Find files related to the website project").text == "website project"


def test_every_understood_part_is_reported_back() -> None:
    understood = parse("python files larger than 1 GB before 2023").understood
    assert "python files" in understood
    assert "larger than 1 GB" in understood
    assert "modified before 2023" in understood


def test_explicit_ui_filters_override_parsed_ones() -> None:
    merged = merge_queries(parse("images before 2023"), FileQuery(categories=("pdf",), limit=10))
    assert merged.categories == ("pdf",)
    assert merged.modified_before == datetime(2023, 1, 1, tzinfo=UTC)
    assert merged.limit == 10
