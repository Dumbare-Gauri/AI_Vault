"""Where the agent may act, and on what.

Every operation names a path, and every path is checked here before anything
touches the disk. The rules follow disktree's removal guards
(https://github.com/tobi/disktree, MIT — see DISKTREE_LICENSE): act only
inside folders the user authorized, never on the authorized folder itself,
never on the filesystem root, the home directory or anything holding it,
never inside the operating system's own trees, never across a mount point,
and never through a link that leads somewhere else. Names Windows would read
as a different file are refused outright.
"""

import os
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
_CASE_INSENSITIVE = _IS_WINDOWS or sys.platform == "darwin"
_MAX_NAME_LENGTH = 255

# Trees the operating system owns (adapted from disktree's SYSTEM_TREES).
_POSIX_SYSTEM_TREES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/lib32", "/libx32",
    "/nix/store", "/gnu/store", "/proc", "/run", "/sbin", "/sys", "/usr",
    "/var/lib", "/efi", "/home/linuxbrew/.linuxbrew", "/System", "/Library",
    "/private", "/cores", "/opt/homebrew", "/Network",
)  # fmt: skip
_WINDOWS_SYSTEM_ENV = (
    "SystemRoot",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "ProgramData",
)
_WINDOWS_SYSTEM_NAMES = (
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "System Volume Information", "$Recycle.Bin", "Recovery", "Boot", "bootmgr",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
)  # fmt: skip
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"}


class PathRefused(Exception):
    """A path or name the agent will not act on, with the reason in plain words."""


def path_key(path: Path | str) -> str:
    """How paths are compared: normalized, and case-folded where the platform
    ignores case, so a different spelling can't slip past a guard."""
    normalized = os.path.normpath(str(path))
    return normalized.lower() if _CASE_INSENSITIVE else normalized


def _within(path: Path | str, base: Path | str) -> bool:
    candidate, base_key = path_key(path), path_key(base)
    if candidate == base_key:
        return True
    prefix = base_key if base_key.endswith(os.sep) else base_key + os.sep
    return candidate.startswith(prefix)


def _system_trees() -> list[str]:
    if not _IS_WINDOWS:
        return list(_POSIX_SYSTEM_TREES)
    trees = [
        value
        for name in _WINDOWS_SYSTEM_ENV
        if (value := os.environ.get(name)) and os.path.isabs(value)
    ]
    drive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    trees += [os.path.join(drive, name) for name in _WINDOWS_SYSTEM_NAMES]
    return trees


def misread_by_windows(name: str) -> bool:
    """A name Win32 opens as some other file: trailing dots or spaces are
    stripped, `NUL`/`COM1`… are devices, and a colon starts a stream."""
    if name.endswith((".", " ")) or ":" in name:
        return True
    stem = name.split(".")[0].rstrip().lower()
    numbered = stem[:3] in ("com", "lpt") and stem[3:] in {str(n) for n in range(1, 10)}
    return numbered or stem in _WINDOWS_DEVICES


def _system_drive() -> str:
    if _IS_WINDOWS:
        return os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    return "/"


def is_volume_system_folder(path: Path) -> bool:
    """The folders Windows keeps at the top of every drive, external ones
    included (the Recycle Bin, restore points)."""
    return (
        _IS_WINDOWS
        and path.parent.parent == path.parent
        and path.name.lower() in ("$recycle.bin", "system volume information")
    )


class AuthorizedRoots:
    """The folders the user allowed the agent to work in."""

    def __init__(self, roots: list[Path]) -> None:
        self._home = Path.home().resolve()
        self._system = _system_trees()
        self.roots: list[Path] = []
        for root in roots:
            self.add(root)

    def refusal(self, root: Path | str) -> str | None:
        """Why a folder can't be given to the agent, or None if it can. The
        whole system drive and the system's own folders never can; any other
        drive — an external one — can, as a whole or in part."""
        path = Path(root)
        if not path.is_absolute():
            return "Only absolute paths are accepted."
        if not path.is_dir():
            return f"{path} isn't a folder that exists"
        path = path.resolve()
        if path_key(path) == path_key(_system_drive()):
            return f"{path} is the whole system drive — choose a folder inside it"
        if self.system_tree(path) or any(_within(tree, path) for tree in self._system):
            return f"{path} is part of the system and cannot be authorized"
        covering = next((root for root in self.roots if _within(path, root)), None)
        if covering is not None:
            return f"{path} is already covered by {covering}"
        return None

    def add(self, root: Path | str) -> Path:
        problem = self.refusal(root)
        if problem:
            raise PathRefused(problem)
        path = Path(root).resolve(strict=True)
        for existing in self.roots:
            if _within(existing, path):
                raise PathRefused(f"{path} already holds {existing} — remove that one first")
        self.roots.append(path)
        return path

    def remove(self, root: Path | str) -> Path:
        key = path_key(Path(root).resolve())
        match = next((r for r in self.roots if path_key(r) == key), None)
        if match is None:
            raise PathRefused(f"{root} isn't one of the agent's folders")
        self.roots.remove(match)
        return match

    def covers(self, root: Path | str) -> bool:
        return any(path_key(r) == path_key(Path(root).resolve()) for r in self.roots)

    def check(self, path: Path | str, *, for_change: bool = False) -> Path:
        """The real path to act on, or `PathRefused`. `for_change` adds the
        rules for anything that modifies the disk."""
        candidate = Path(path)
        if not candidate.is_absolute():
            raise PathRefused("Only absolute paths are accepted.")
        lexical = Path(os.path.normpath(candidate))
        root = next((r for r in self.roots if _within(lexical, r)), None)
        if root is None:
            raise PathRefused(f"{lexical} is outside every authorized folder")

        real = Path(os.path.realpath(lexical))
        expected = root / os.path.relpath(lexical, root)
        if path_key(real) != path_key(expected):
            raise PathRefused(f"{lexical} is reached through a link: it is really {real}")

        if for_change:
            if path_key(real) == path_key(self._home):
                raise PathRefused("The home directory itself cannot be changed")
            if _within(self._home, real):
                raise PathRefused(f"{real} contains the home directory")
            if path_key(real) == path_key(root):
                raise PathRefused("An authorized root folder itself cannot be changed")
            if system := self.system_tree(real):
                raise PathRefused(f"{real} is part of the system under {system}")
            if real.exists() and os.path.ismount(real):
                raise PathRefused(f"{real} is a mount point: it belongs to another volume")
        return real

    def check_new_name(self, name: str) -> str:
        """A name for something being created or renamed: one plain path
        component that every platform reads the same way."""
        if not name or name in (".", "..") or len(name) > _MAX_NAME_LENGTH:
            raise PathRefused("That isn't a usable file name.")
        if "/" in name or "\\" in name or any(ord(ch) < 32 for ch in name):
            raise PathRefused("A name can't contain slashes or control characters.")
        if misread_by_windows(name):
            raise PathRefused(f"Windows would read {name!r} as a different name.")
        return name

    def root_of(self, path: Path) -> Path:
        return next(r for r in self.roots if _within(path, r))

    def system_tree(self, path: Path) -> str | None:
        if _within(path, self._home):
            return None
        for folder in (path, *path.parents):
            if is_volume_system_folder(folder):
                return str(folder)
        return next((tree for tree in self._system if _within(path, tree)), None)
