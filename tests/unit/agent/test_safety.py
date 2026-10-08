import os
import sys
from pathlib import Path

import pytest
from aivault_agent.safety import AuthorizedRoots, PathRefused


@pytest.fixture
def roots(tmp_path: Path) -> AuthorizedRoots:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "notes.txt").write_text("hi")
    return AuthorizedRoots([tmp_path / "work"])


def test_a_path_inside_an_authorized_root_is_accepted(
    roots: AuthorizedRoots, tmp_path: Path
) -> None:
    resolved = roots.check(tmp_path / "work" / "notes.txt")

    assert resolved == (tmp_path / "work" / "notes.txt").resolve()


def test_a_path_outside_every_root_is_refused(roots: AuthorizedRoots, tmp_path: Path) -> None:
    (tmp_path / "elsewhere.txt").write_text("x")

    with pytest.raises(PathRefused, match="outside"):
        roots.check(tmp_path / "elsewhere.txt")


def test_dot_dot_cannot_escape_a_root(roots: AuthorizedRoots, tmp_path: Path) -> None:
    with pytest.raises(PathRefused, match="outside"):
        roots.check(tmp_path / "work" / ".." / "elsewhere.txt")


def test_the_root_itself_cannot_be_changed(roots: AuthorizedRoots, tmp_path: Path) -> None:
    with pytest.raises(PathRefused, match="root"):
        roots.check(tmp_path / "work", for_change=True)


def test_the_root_itself_can_be_read(roots: AuthorizedRoots, tmp_path: Path) -> None:
    assert roots.check(tmp_path / "work") == (tmp_path / "work").resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_link_leading_outside_the_root_is_refused(roots: AuthorizedRoots, tmp_path: Path) -> None:
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", tmp_path / "work" / "shortcut")

    with pytest.raises(PathRefused, match="link"):
        roots.check(tmp_path / "work" / "shortcut" / "file.txt")


def test_the_system_drive_can_never_be_authorized_as_a_whole() -> None:
    with pytest.raises(PathRefused, match="whole system drive"):
        AuthorizedRoots([Path(Path.home().anchor)])


def test_a_system_directory_can_never_be_authorized() -> None:
    system = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        if sys.platform == "win32"
        else Path("/usr")
    )

    with pytest.raises(PathRefused, match="system"):
        AuthorizedRoots([system])


def test_the_home_directory_itself_cannot_be_changed(tmp_path: Path) -> None:
    home = Path.home()
    roots = AuthorizedRoots([home])

    with pytest.raises(PathRefused, match="home"):
        roots.check(home, for_change=True)


@pytest.mark.parametrize("name", ["report.", "notes ", "CON", "nul.txt", "COM1", "a:stream"])
def test_names_windows_would_misread_are_refused(
    roots: AuthorizedRoots, tmp_path: Path, name: str
) -> None:
    with pytest.raises(PathRefused):
        roots.check_new_name(name)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "line\nbreak"])
def test_names_that_are_not_a_single_safe_component_are_refused(
    roots: AuthorizedRoots, name: str
) -> None:
    with pytest.raises(PathRefused):
        roots.check_new_name(name)


def test_an_ordinary_name_is_accepted(roots: AuthorizedRoots) -> None:
    assert (
        roots.check_new_name("Blarrow_Proposal_2026_Final.pdf") == "Blarrow_Proposal_2026_Final.pdf"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_a_junction_leading_outside_the_root_is_refused(
    roots: AuthorizedRoots, tmp_path: Path
) -> None:
    import _winapi

    (tmp_path / "secret").mkdir()
    _winapi.CreateJunction(str(tmp_path / "secret"), str(tmp_path / "work" / "shortcut"))

    with pytest.raises(PathRefused, match="link"):
        roots.check(tmp_path / "work" / "shortcut" / "file.txt")
