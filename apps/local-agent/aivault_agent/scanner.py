"""Walking an authorized folder and describing what is there.

The accounting follows disktree (https://github.com/tobi/disktree, MIT — see
DISKTREE_LICENSE): links and junctions are reported but never followed, the
walk never crosses onto another volume, a hardlinked file counts once in the
totals, and both the apparent size (what `ls -l` shows) and the space the
file actually takes on disk are reported. A folder that can't be read is
listed as an error rather than guessed at.
"""

import hashlib
import mimetypes
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aivault_agent.safety import AuthorizedRoots

_IS_WINDOWS = sys.platform == "win32"
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_HASH_CHUNK = 1024 * 1024
# The agent's own Trash inside each authorized folder (see operations.py),
# never reported as part of the user's files.
TRASH_DIR = ".aivault-trash"
# Folders tools create and manage — dependencies, caches, version control.
# They aren't the user's files to organize and can hold millions of entries.
# Any folder whose name starts with a dot is left out for the same reason.
_TOOL_FOLDERS = frozenset(
    {"node_modules", "bower_components", "__pycache__", "venv", "site-packages", "$recycle.bin"}
)


def is_tool_folder(name: str) -> bool:
    return name.startswith(".") or name.lower() in _TOOL_FOLDERS


@dataclass(frozen=True)
class Entry:
    # Stable across rename and move on the same volume: (device, file id).
    id: str
    parent_id: str | None
    relative_path: str
    name: str
    is_dir: bool
    is_link: bool
    hidden: bool
    extension: str | None
    mime_type: str | None
    size_bytes: int
    allocated_bytes: int
    created_at: datetime | None
    modified_at: datetime | None
    accessed_at: datetime | None
    read_only: bool
    owner: str | None
    sha256: str | None = None


@dataclass
class ScanResult:
    root: Path
    entries: list[Entry] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    apparent_bytes: int = 0
    allocated_bytes: int = 0


def identity(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}"


def _time(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value else None


def _created(info: os.stat_result) -> float | None:
    birth = getattr(info, "st_birthtime", None)
    return birth if birth is not None else (info.st_ctime if _IS_WINDOWS else None)


def _owner(info: os.stat_result) -> str | None:
    if _IS_WINDOWS:
        return None
    try:
        import pwd

        return pwd.getpwuid(info.st_uid).pw_name
    except (ImportError, KeyError):
        return None


def _cluster_size(path: Path) -> int:
    if not _IS_WINDOWS:
        return 512
    import ctypes

    sectors, bytes_per_sector = ctypes.c_ulong(), ctypes.c_ulong()
    free, total = ctypes.c_ulong(), ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(  # type: ignore[attr-defined]
        str(path.anchor), ctypes.byref(sectors), ctypes.byref(bytes_per_sector),
        ctypes.byref(free), ctypes.byref(total),
    )  # fmt: skip
    return sectors.value * bytes_per_sector.value if ok else 4096


def _allocated(info: os.stat_result, cluster: int) -> int:
    blocks = getattr(info, "st_blocks", None)
    if blocks is not None:
        return blocks * 512
    # Windows reports no block count; a file takes whole clusters.
    return -(-info.st_size // cluster) * cluster if info.st_size else 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def describe(path: Path, root: Path, *, parent_id: str | None, cluster: int) -> Entry:
    """One file or folder, described without following a link it may be."""
    info = os.stat(path, follow_symlinks=False)
    attributes = getattr(info, "st_file_attributes", 0)
    is_link = stat.S_ISLNK(info.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    is_dir = stat.S_ISDIR(info.st_mode) and not is_link
    name = path.name
    extension = name.rsplit(".", 1)[1].lower() if "." in name.strip(".") and not is_dir else None
    return Entry(
        id=identity(info),
        parent_id=parent_id,
        relative_path=path.relative_to(root).as_posix(),
        name=name,
        is_dir=is_dir,
        is_link=is_link,
        hidden=name.startswith(".") or bool(attributes & _FILE_ATTRIBUTE_HIDDEN),
        extension=extension,
        mime_type=None if is_dir else mimetypes.guess_type(name)[0],
        size_bytes=0 if is_dir else info.st_size,
        allocated_bytes=0 if is_dir else _allocated(info, cluster),
        created_at=_time(_created(info)),
        modified_at=_time(info.st_mtime),
        accessed_at=_time(info.st_atime),
        read_only=not os.access(path, os.W_OK),
        owner=_owner(info),
    )


def walk(
    root: Path, *, root_id: str, device: int, cluster: int, result: ScanResult
) -> Iterator[Entry]:
    pending: list[tuple[Path, str]] = [(root, root_id)]
    while pending:
        folder, folder_id = pending.pop()
        try:
            children = sorted(os.scandir(folder), key=lambda child: child.name)
        except OSError as error:
            result.errors.append(
                (folder.relative_to(root).as_posix() or ".", error.strerror or str(error))
            )
            continue
        for child in children:
            try:
                if child.is_dir(follow_symlinks=False) and is_tool_folder(child.name):
                    continue
            except OSError:
                pass
            path = Path(child.path)
            try:
                entry = describe(path, root, parent_id=folder_id, cluster=cluster)
                child_device = os.stat(path, follow_symlinks=False).st_dev
            except OSError as error:
                result.errors.append(
                    (path.relative_to(root).as_posix(), error.strerror or str(error))
                )
                continue
            yield entry
            if entry.is_dir and child_device == device:
                pending.append((path, entry.id))


def scan(root: Path, roots: AuthorizedRoots, *, hash_max_bytes: int | None = None) -> ScanResult:
    """Everything under `root`, which must be an authorized folder."""
    real_root = roots.check(root)
    root_info = os.stat(real_root)
    cluster = _cluster_size(real_root)
    result = ScanResult(root=real_root)
    counted: set[str] = set()
    for entry in walk(
        real_root,
        root_id=identity(root_info),
        device=root_info.st_dev,
        cluster=cluster,
        result=result,
    ):
        hashable = not entry.is_dir and not entry.is_link
        if hash_max_bytes is not None and hashable and entry.size_bytes <= hash_max_bytes:
            try:
                entry = _with_hash(entry, real_root)
            except OSError as error:
                result.errors.append((entry.relative_path, error.strerror or str(error)))
        result.entries.append(entry)
        if not entry.is_dir and entry.id not in counted:
            counted.add(entry.id)
            result.apparent_bytes += entry.size_bytes
            result.allocated_bytes += entry.allocated_bytes
    return result


def _with_hash(entry: Entry, root: Path) -> Entry:
    from dataclasses import replace

    return replace(entry, sha256=_sha256(root / entry.relative_path))
