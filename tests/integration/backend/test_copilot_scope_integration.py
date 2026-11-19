"""Ask Vault answers about where the user is: the folder on screen, the files
they selected — never an earlier screen's files, never the other storage.
Disposable index data only; no real storage."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_storage_isolation_integration import _TwoWorkspaces, requires_infra

from app.application.conversation_service import PageContext
from vault_shared import ValidationError
from vault_shared.db.models import ExecutionActionType
from vault_shared.db.repositories import ExecutionStepRepository, FolderRepository
from vault_shared.db.session import get_session_factory


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def both(db: Session) -> _TwoWorkspaces:
    return _TwoWorkspaces(db)


def _folder(both: _TwoWorkspaces, where: str, path: str):  # noqa: ANN202
    source = both.drive_source if where == "drive" else both.local_source
    folder = FolderRepository(both.db).upsert(
        storage_source_id=source.id,
        provider_file_id=f"d-{uuid.uuid4().hex[:8]}",
        provider_parent_id=None,
        parent_folder_id=None,
        name=path.rsplit("/", 1)[-1],
        path=path,
        owner_email=None,
        is_shared=False,
        provider_created_at=None,
        provider_modified_at=None,
        scanned_at=datetime.now(UTC),
    )
    both.db.commit()
    return folder


def _in(folder, *, selected=()) -> PageContext:  # noqa: ANN001
    return PageContext(
        page="files",
        label="anything the client says",
        folder_id=str(folder.id),
        source_id=str(folder.storage_source_id),
        selected_file_ids=tuple(str(file.id) for file in selected),
    )


def _cited(turn) -> set[uuid.UUID]:  # noqa: ANN001
    return {citation.file_id for citation in turn.citations}


def _listed(turn) -> set[str]:  # noqa: ANN001
    return {
        item["id"] for block in turn.assistant_message.blocks for item in block.get("items", [])
    }


_TEXT = "Quarterly report for the Database migration. Totals and owners are listed."


@requires_infra
def test_summarizing_all_files_uses_only_the_folder_on_screen(both: _TwoWorkspaces) -> None:
    database = _folder(both, "local", "/Database")
    inside = [
        both.file("local", "/Database/schema.pdf", text=_TEXT),
        both.file("local", "/Database/backup/plan.pdf", text=_TEXT),
    ]
    elsewhere = both.file("local", "/TheFramedWall/report.pdf", text=_TEXT)

    turn = both.ask("sumarize me all the files", "local", page=_in(database))

    assert _cited(turn) and _cited(turn) <= {file.id for file in inside}
    assert _listed(turn) == {str(file.id) for file in inside}
    assert str(elsewhere.id) not in _listed(turn)


@requires_infra
def test_the_coverage_of_a_folder_summary_is_stated(both: _TwoWorkspaces) -> None:
    database = _folder(both, "local", "/Database")
    both.file("local", "/Database/schema.pdf", text=_TEXT)

    turn = both.ask("summarize all the files", "local", page=_in(database))

    notices = [b["text"] for b in turn.assistant_message.blocks if b["type"] == "notice"]
    assert any("C:\\Work\\Database" in text for text in notices)


@requires_infra
def test_summarize_these_uses_only_the_selected_files(both: _TwoWorkspaces) -> None:
    database = _folder(both, "local", "/Database")
    picked = both.file("local", "/Database/schema.pdf", text=_TEXT)
    both.file("local", "/Database/other.pdf", text=_TEXT)

    turn = both.ask("Summarize these files", "local", page=_in(database, selected=[picked]))

    assert _cited(turn) == {picked.id}


@requires_infra
def test_these_with_nothing_selected_asks_which_files(both: _TwoWorkspaces) -> None:
    database = _folder(both, "local", "/Database")
    both.file("local", "/Database/schema.pdf", text=_TEXT)

    turn = both.ask("Summarize these files", "local", page=_in(database))

    assert "No files are selected" in turn.assistant_message.content
    assert turn.citations == []


@requires_infra
def test_an_earlier_folders_results_never_answer_for_another_folder(both: _TwoWorkspaces) -> None:
    first = _folder(both, "local", "/Invoices")
    second = _folder(both, "local", "/Photos")
    invoice = both.file("local", "/Invoices/blarrow invoice.pdf")
    both.file("local", "/Photos/beach.pdf")
    earlier = both.ask("find pdf files", "local", page=_in(first))

    turn = both.ask(
        "archive them", "local", conversation_id=earlier.conversation.id, page=_in(second)
    )

    assert str(invoice.id) not in _listed(turn)


@requires_infra
def test_duplicates_are_found_within_the_folder_on_screen(both: _TwoWorkspaces) -> None:
    docs = _folder(both, "local", "/project_docs")
    here = [
        both.file("local", "/project_docs/spec.pdf", checksum="docs-dup"),
        both.file("local", "/project_docs/old/spec.pdf", checksum="docs-dup"),
    ]
    both.file("local", "/Other/a.pdf", checksum="other-dup")
    both.file("local", "/Other/b.pdf", checksum="other-dup")
    both.analyze()

    turn = both.ask("find duplicates", "local", page=_in(docs))

    [block] = [b for b in turn.assistant_message.blocks if b["type"] == "duplicate_groups"]
    assert block["total_groups"] == 1
    assert {f["id"] for group in block["groups"] for f in group["files"]} == {
        str(file.id) for file in here
    }


@requires_infra
def test_organizing_a_folder_keeps_the_new_folders_inside_it(both: _TwoWorkspaces) -> None:
    docs = _folder(both, "local", "/project_docs")
    loose = both.file("local", "/project_docs/notes.pdf")
    loose.parent_folder_id = docs.id
    both.db.commit()

    turn = both.ask("organize this folder", "local", page=_in(docs))

    assert "pending" in (turn.conversation.state or {}), turn.assistant_message.content
    [proposal] = turn.conversation.state["pending"].values()
    assert proposal["parent_folder_id"] == str(docs.id)


@requires_infra
def test_a_folder_from_the_other_storage_is_ignored(both: _TwoWorkspaces) -> None:
    drive_folder = _folder(both, "drive", "/Finance")
    drive_file = both.file("drive", "/Finance/invoice.pdf", text=_TEXT)
    both.file("local", "/Notes/report.pdf", text=_TEXT)

    turn = both.ask("summarize all the files", "local", page=_in(drive_folder))

    assert drive_file.id not in _cited(turn)
    assert str(drive_file.id) not in _listed(turn)


@requires_infra
def test_an_archive_destination_in_another_storage_is_refused(both: _TwoWorkspaces) -> None:
    local_file = both.file("local", "/Notes/report.pdf")
    drive_folder = _folder(both, "drive", "/Backups")

    with pytest.raises(ValidationError, match="same storage"):
        both.plans().create_ad_hoc_plan(
            [local_file.id],
            action_type=ExecutionActionType.CREATE_ARCHIVE,
            organization_id=both.org.id,
            user_id=both.user.id,
            connector_id=both.local.id,
            archive_folder_id=drive_folder.id,
        )


@requires_infra
def test_an_unusable_archive_name_is_refused(both: _TwoWorkspaces) -> None:
    local_file = both.file("local", "/Notes/report.pdf")

    with pytest.raises(ValidationError):
        both.plans().create_ad_hoc_plan(
            [local_file.id],
            action_type=ExecutionActionType.CREATE_ARCHIVE,
            organization_id=both.org.id,
            user_id=both.user.id,
            connector_id=both.local.id,
            archive_name="../escape",
        )


@requires_infra
def test_a_chosen_archive_name_and_folder_are_planned(both: _TwoWorkspaces) -> None:
    local_file = both.file("local", "/Notes/report.pdf")
    backups = _folder(both, "local", "/Backups")

    plan = both.plans().create_ad_hoc_plan(
        [local_file.id],
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        organization_id=both.org.id,
        user_id=both.user.id,
        connector_id=both.local.id,
        archive_name="Project Docs",
        archive_folder_id=backups.id,
    )

    [step] = ExecutionStepRepository(both.db).list_for_plan(plan.id)
    assert step.planned_change["archive_name"] == "Project Docs.zip"
    assert step.planned_change["archive_folder_id"] == str(backups.id)
