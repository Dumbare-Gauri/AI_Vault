from datetime import date

import pytest

from worker.organization.rename_proposal import is_uninformative_name, propose_name


@pytest.mark.parametrize(
    "name",
    [
        "Untitled.txt",
        "Untitled document",
        "Copy of Untitled.docx",
        "Document1.docx",
        "IMG_2041.jpg",
        "Scan 3.pdf",
        "notes (1).md",
        "final final.pdf",
        "New Text Document.txt",
        "Screenshot 2026-03-01 at 10.22.31.png",
    ],
)
def test_flags_names_that_say_nothing_about_the_file(name: str) -> None:
    assert is_uninformative_name(name)


@pytest.mark.parametrize(
    "name",
    ["Acme Q3 invoice.pdf", "Phoenix campaign brief v2.md", "budget-2026.xlsx", "README.md"],
)
def test_leaves_descriptive_names_alone(name: str) -> None:
    assert not is_uninformative_name(name)


def test_builds_the_name_from_entity_document_type_and_month() -> None:
    proposed = propose_name(
        current_name="Untitled.txt",
        entity_name="Acme Corp",
        document_type="meeting notes",
        dated=date(2026, 3, 14),
    )

    assert proposed == "Acme Corp - Meeting Notes - 2026-03.txt"


def test_keeps_the_original_extension() -> None:
    proposed = propose_name(
        current_name="IMG_2041.JPG", entity_name="Phoenix", document_type=None, dated=None
    )

    assert proposed == "Phoenix.JPG"


def test_proposes_nothing_without_evidence() -> None:
    assert (
        propose_name(current_name="Untitled.txt", entity_name=None, document_type=None, dated=None)
        is None
    )


def test_strips_characters_that_cannot_appear_in_a_name() -> None:
    proposed = propose_name(
        current_name="scan.pdf", entity_name="R&D / Labs", document_type="invoice", dated=None
    )

    assert proposed == "R&D Labs - Invoice.pdf"


def test_never_proposes_an_unchanged_name() -> None:
    assert (
        propose_name(
            current_name="Phoenix.md", entity_name="Phoenix", document_type=None, dated=None
        )
        is None
    )


def test_unknown_document_type_is_not_used_as_evidence() -> None:
    assert (
        propose_name(current_name="doc1.pdf", entity_name=None, document_type="unknown", dated=None)
        is None
    )
