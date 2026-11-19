"""Archiving and extracting on this computer, against real files in a
throwaway folder: the archive keeps folder structure, is checked before it
is kept, never overwrites, and extraction can't write outside its folder."""

import shutil
import zipfile
from pathlib import Path

import pytest
from aivault_agent import operations
from aivault_agent.operations import LocalOperations, OperationFailed
from aivault_agent.safety import AuthorizedRoots
from aivault_agent.scanner import scan


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "Project" / "docs").mkdir(parents=True)
    (root / "Project" / "docs" / "notes.txt").write_text("notes " * 500, encoding="utf-8")
    (root / "Project" / "photo.jpg").write_bytes(bytes(range(256)) * 40)
    (root / "Other" / "docs").mkdir(parents=True)
    # The same name in another folder stays a different file.
    (root / "Other" / "docs" / "notes.txt").write_text("other notes", encoding="utf-8")
    (root / "Archives").mkdir()
    return root


@pytest.fixture
def ops(root: Path) -> LocalOperations:
    return LocalOperations(AuthorizedRoots([root]))


def _id(ops: LocalOperations, root: Path, relative: str) -> str:
    return next(e.id for e in scan(root, ops.roots).entries if e.relative_path == relative)


def _items(ops: LocalOperations, root: Path, *pairs: tuple[str, str]) -> list:
    return [(relative, _id(ops, root, relative), arcname) for relative, arcname in pairs]


def test_an_archive_keeps_the_folder_structure_and_passes_its_check(
    ops: LocalOperations, root: Path
) -> None:
    items = _items(
        ops,
        root,
        ("Project/docs/notes.txt", "Project/docs/notes.txt"),
        ("Project/photo.jpg", "Project/photo.jpg"),
        ("Other/docs/notes.txt", "Other/docs/notes.txt"),
    )

    result = ops.create_archive(root, "Archives", "backup.zip", items)

    with zipfile.ZipFile(root / "Archives" / "backup.zip") as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == [
            "Other/docs/notes.txt",
            "Project/docs/notes.txt",
            "Project/photo.jpg",
        ]
        assert archive.read("Other/docs/notes.txt") == b"other notes"
    assert result.entry.sha256 is not None
    assert not (root / "Archives" / "backup.zip.partial").exists()


def test_already_compressed_files_are_stored_not_recompressed(
    ops: LocalOperations, root: Path
) -> None:
    items = _items(
        ops, root, ("Project/photo.jpg", "photo.jpg"), ("Project/docs/notes.txt", "notes.txt")
    )

    result = ops.create_archive(root, "Archives", "mixed.zip", items)

    by_name = {entry.arcname: entry for entry in result.entries}
    assert by_name["photo.jpg"].stored is True
    assert by_name["notes.txt"].stored is False
    assert by_name["notes.txt"].compressed_bytes < by_name["notes.txt"].size_bytes


def test_an_archive_never_overwrites_an_existing_file(ops: LocalOperations, root: Path) -> None:
    (root / "Archives" / "backup.zip").write_bytes(b"keep me")
    items = _items(ops, root, ("Project/photo.jpg", "photo.jpg"))

    with pytest.raises(OperationFailed):
        ops.create_archive(root, "Archives", "backup.zip", items)

    assert (root / "Archives" / "backup.zip").read_bytes() == b"keep me"


def test_an_unsafe_name_inside_the_archive_is_skipped(ops: LocalOperations, root: Path) -> None:
    items = _items(
        ops, root, ("Project/photo.jpg", "../escape.jpg"), ("Project/docs/notes.txt", "notes.txt")
    )

    result = ops.create_archive(root, "Archives", "safe.zip", items)

    assert [entry.arcname for entry in result.entries] == ["notes.txt"]
    assert len(result.skipped) == 1


def test_extraction_recreates_the_folders_without_overwriting(
    ops: LocalOperations, root: Path
) -> None:
    items = _items(
        ops,
        root,
        ("Project/docs/notes.txt", "Project/docs/notes.txt"),
        ("Project/photo.jpg", "Project/photo.jpg"),
    )
    ops.create_archive(root, "Archives", "backup.zip", items)
    archive_id = _id(ops, root, "Archives/backup.zip")

    first = ops.extract_archive(root, "Archives/backup.zip", archive_id, "Archives")
    second = ops.extract_archive(root, "Archives/backup.zip", archive_id, "Archives")

    assert (root / "Archives" / "backup" / "Project" / "docs" / "notes.txt").exists()
    assert first.folder.relative_path == "Archives/backup"
    # A second extraction goes into its own new folder, never over the first.
    assert second.folder.relative_path == "Archives/backup (2)"


def test_extraction_never_writes_outside_its_folder(ops: LocalOperations, root: Path) -> None:
    hostile = root / "Archives" / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr("../../outside.txt", "nope")
        archive.writestr("C:/Windows/evil.txt", "nope")
        archive.writestr("fine/ok.txt", "ok")
    archive_id = _id(ops, root, "Archives/hostile.zip")

    result = ops.extract_archive(root, "Archives/hostile.zip", archive_id, "Archives")

    assert not (root.parent / "outside.txt").exists()
    assert (root / "Archives" / "hostile" / "fine" / "ok.txt").read_text() == "ok"
    assert len(result.skipped) == 2


def test_an_archive_too_big_for_the_free_space_is_refused_before_writing(
    ops: LocalOperations, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil.disk_usage(root)
    monkeypatch.setattr(
        operations.shutil, "disk_usage", lambda _path: usage._replace(free=1024)
    )
    items = _items(ops, root, ("Project/photo.jpg", "photo.jpg"))

    with pytest.raises(OperationFailed, match="Not enough free space"):
        ops.create_archive(root, "Archives", "full.zip", items)

    assert list((root / "Archives").iterdir()) == []


def test_a_file_over_two_gigabytes_is_archived_with_zip64(
    ops: LocalOperations, root: Path
) -> None:
    big = root / "Project" / "disk.img"
    with open(big, "wb") as handle:
        handle.truncate(2 * 1024**3 + 1024 * 1024)
    items = _items(ops, root, ("Project/disk.img", "Project/disk.img"))

    result = ops.create_archive(root, "Archives", "big.zip", items)

    assert result.entries[0].size_bytes == big.stat().st_size
    with zipfile.ZipFile(root / "Archives" / "big.zip") as archive:
        assert archive.getinfo("Project/disk.img").file_size == big.stat().st_size


def test_listing_an_archive_flags_unsafe_entries_and_writes_nothing(
    ops: LocalOperations, root: Path
) -> None:
    with zipfile.ZipFile(root / "Archives" / "mixed.zip", "w") as archive:
        archive.writestr("docs/notes.txt", "notes")
        archive.writestr("../escape.txt", "nope")
    before = sorted(p.name for p in (root / "Archives").iterdir())

    contents = ops.archive_contents(root, "Archives/mixed.zip", _id(ops, root, "Archives/mixed.zip"))

    assert contents["file_count"] == 2
    assert contents["unsafe_count"] == 1
    assert {e["name"]: e["safe"] for e in contents["entries"]} == {
        "docs/notes.txt": True,
        "../escape.txt": False,
    }
    assert sorted(p.name for p in (root / "Archives").iterdir()) == before
