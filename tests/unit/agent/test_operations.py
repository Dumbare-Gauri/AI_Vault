import hashlib
from pathlib import Path

import pytest
from aivault_agent.operations import LocalOperations, OperationFailed
from aivault_agent.safety import AuthorizedRoots, PathRefused
from aivault_agent.scanner import scan


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "Inbox").mkdir(parents=True)
    (root / "Inbox" / "proposal-final2.pdf").write_bytes(b"%PDF proposal v2")
    return root


@pytest.fixture
def ops(root: Path) -> LocalOperations:
    return LocalOperations(AuthorizedRoots([root]))


def _id_of(ops: LocalOperations, root: Path, relative: str) -> str:
    return next(e.id for e in scan(root, ops.roots).entries if e.relative_path == relative)


def test_rename_changes_the_real_file_and_keeps_its_identity(
    ops: LocalOperations, root: Path
) -> None:
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    result = ops.rename(
        root, "Inbox/proposal-final2.pdf", file_id, "Blarrow_Proposal_2026_Final.pdf"
    )

    assert (root / "Inbox" / "Blarrow_Proposal_2026_Final.pdf").read_bytes() == b"%PDF proposal v2"
    assert not (root / "Inbox" / "proposal-final2.pdf").exists()
    assert result.entry.id == file_id
    assert result.entry.relative_path == "Inbox/Blarrow_Proposal_2026_Final.pdf"


def test_rename_never_overwrites(ops: LocalOperations, root: Path) -> None:
    (root / "Inbox" / "taken.pdf").write_bytes(b"other")
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    with pytest.raises(OperationFailed, match="already exists"):
        ops.rename(root, "Inbox/proposal-final2.pdf", file_id, "taken.pdf")

    assert (root / "Inbox" / "taken.pdf").read_bytes() == b"other"


def test_a_stale_identity_is_refused(ops: LocalOperations, root: Path) -> None:
    with pytest.raises(OperationFailed, match="no longer"):
        ops.rename(root, "Inbox/proposal-final2.pdf", "1:999999", "x.pdf")


def test_create_folder_and_move_into_it(ops: LocalOperations, root: Path) -> None:
    folder = ops.create_folder(root, "", "Blarrow")
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    moved = ops.move(root, "Inbox/proposal-final2.pdf", file_id, "Blarrow")

    assert folder.entry.is_dir and (root / "Blarrow").is_dir()
    assert (root / "Blarrow" / "proposal-final2.pdf").exists()
    assert not (root / "Inbox" / "proposal-final2.pdf").exists()
    assert moved.entry.id == file_id


def test_create_file_writes_exactly_the_content(ops: LocalOperations, root: Path) -> None:
    result = ops.create_file(root, "Inbox", "notes.md", "# Kickoff\n")

    assert (root / "Inbox" / "notes.md").read_bytes() == b"# Kickoff\n"
    assert result.entry.sha256 == hashlib.sha256(b"# Kickoff\n").hexdigest()


def test_copy_is_byte_identical_and_separate(ops: LocalOperations, root: Path) -> None:
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    result = ops.copy(root, "Inbox/proposal-final2.pdf", file_id, "", "proposal-copy.pdf")

    assert (root / "proposal-copy.pdf").read_bytes() == b"%PDF proposal v2"
    assert result.entry.id != file_id


def test_trash_then_restore_puts_the_file_back_exactly(ops: LocalOperations, root: Path) -> None:
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    trashed = ops.trash(root, "Inbox/proposal-final2.pdf", file_id)
    assert not (root / "Inbox" / "proposal-final2.pdf").exists()
    assert "Inbox/proposal-final2.pdf" not in {
        e.relative_path for e in scan(root, ops.roots).entries
    }

    restored = ops.restore(root, trashed.trash_id)

    assert (root / "Inbox" / "proposal-final2.pdf").read_bytes() == b"%PDF proposal v2"
    assert restored.entry.id == file_id


def test_permanent_delete_needs_the_item_in_trash_first(ops: LocalOperations, root: Path) -> None:
    file_id = _id_of(ops, root, "Inbox/proposal-final2.pdf")

    with pytest.raises(OperationFailed, match="Trash"):
        ops.permanent_delete(root, "not-a-trash-id", confirm=True)

    trashed = ops.trash(root, "Inbox/proposal-final2.pdf", file_id)
    with pytest.raises(OperationFailed, match="confirm"):
        ops.permanent_delete(root, trashed.trash_id, confirm=False)

    ops.permanent_delete(root, trashed.trash_id, confirm=True)

    assert ops.list_trash(root) == []
    assert not any((root / ".aivault-trash").iterdir())


def test_operations_outside_the_authorized_folder_are_refused(
    ops: LocalOperations, tmp_path: Path
) -> None:
    (tmp_path / "outside.txt").write_text("x")

    with pytest.raises(PathRefused):
        ops.create_folder(tmp_path, "", "nope")


def test_dangerous_names_are_refused_before_touching_the_disk(
    ops: LocalOperations, root: Path
) -> None:
    with pytest.raises(PathRefused):
        ops.create_folder(root, "", "../escape")

    assert not (root.parent / "escape").exists()
