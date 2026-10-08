import hashlib
import os
import sys
from pathlib import Path

import pytest
from aivault_agent.safety import AuthorizedRoots
from aivault_agent.scanner import scan
from aivault_agent.volumes import list_volumes, volume_for


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    (root / "Blarrow").mkdir(parents=True)
    (root / "Blarrow" / "proposal.pdf").write_bytes(b"%PDF proposal")
    (root / "Blarrow" / "proposal copy.pdf").write_bytes(b"%PDF proposal")
    (root / "notes.md").write_text("# notes\n")
    (root / ".hidden-config").write_text("secret=1\n")
    return root


def _scan(root: Path, **kwargs):  # noqa: ANN003
    return scan(root, AuthorizedRoots([root]), **kwargs)


def test_every_file_and_folder_is_reported_with_its_place(tree: Path) -> None:
    result = _scan(tree)

    paths = {entry.relative_path for entry in result.entries}
    assert paths == {
        "Blarrow",
        "Blarrow/proposal.pdf",
        "Blarrow/proposal copy.pdf",
        "notes.md",
        ".hidden-config",
    }
    pdf = next(e for e in result.entries if e.relative_path == "Blarrow/proposal.pdf")
    folder = next(e for e in result.entries if e.relative_path == "Blarrow")
    assert pdf.parent_id == folder.id
    assert (pdf.extension, pdf.mime_type, pdf.size_bytes) == ("pdf", "application/pdf", 13)
    assert pdf.modified_at is not None


def test_ids_survive_a_rename(tree: Path) -> None:
    before = {e.relative_path: e.id for e in _scan(tree).entries}
    os.rename(tree / "notes.md", tree / "renamed.md")

    after = {e.relative_path: e.id for e in _scan(tree).entries}

    assert after["renamed.md"] == before["notes.md"]


def test_hidden_files_are_marked(tree: Path) -> None:
    hidden = {e.relative_path for e in _scan(tree).entries if e.hidden}

    assert ".hidden-config" in hidden


def test_identical_files_get_identical_hashes(tree: Path) -> None:
    entries = {e.relative_path: e for e in _scan(tree, hash_max_bytes=1024).entries}

    assert entries["Blarrow/proposal.pdf"].sha256 == hashlib.sha256(b"%PDF proposal").hexdigest()
    assert entries["Blarrow/proposal.pdf"].sha256 == entries["Blarrow/proposal copy.pdf"].sha256


def test_a_hardlinked_file_is_counted_once_in_the_totals(tree: Path) -> None:
    os.link(tree / "notes.md", tree / "notes-link.md")

    result = _scan(tree)

    files = [e for e in result.entries if not e.is_dir]
    linked_size = (tree / "notes.md").stat().st_size
    assert result.apparent_bytes == sum(e.size_bytes for e in files) - linked_size


def test_allocated_size_is_reported_alongside_apparent_size(tree: Path) -> None:
    result = _scan(tree)

    assert result.allocated_bytes >= result.apparent_bytes > 0


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_a_junction_is_reported_but_never_followed(tree: Path, tmp_path: Path) -> None:
    import _winapi

    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "private.txt").write_text("x")
    _winapi.CreateJunction(str(tmp_path / "outside"), str(tree / "link"))

    result = _scan(tree)

    paths = {e.relative_path for e in result.entries}
    assert "link" in paths
    assert "link/private.txt" not in paths
    assert next(e for e in result.entries if e.relative_path == "link").is_link


def test_the_volume_holding_a_folder_is_known(tree: Path) -> None:
    volume = volume_for(tree)

    assert volume is not None
    assert volume.total_bytes > 0
    assert 0 <= volume.free_bytes <= volume.total_bytes
    assert any(v.mount == volume.mount for v in list_volumes())


def test_dependency_and_tool_folders_are_left_out(tree: Path) -> None:
    for name in ("node_modules", ".git", ".venv", "__pycache__"):
        (tree / "Blarrow" / name / "deep").mkdir(parents=True)
        (tree / "Blarrow" / name / "deep" / "lib.js").write_text("x")
    (tree / ".notes-config.json").write_text("{}")

    paths = {e.relative_path for e in _scan(tree).entries}

    assert not any(
        part in path for path in paths for part in ("node_modules", ".git", ".venv", "__pycache__")
    )
    assert ".notes-config.json" in paths
