"""The only things the agent will do to the disk.

Each operation is one typed method: it checks every path against the
authorized folders first, never overwrites, never builds a shell command, and
afterwards reads the disk back to confirm the change really happened before
reporting success. Anything else is refused.
"""

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aivault_agent.safety import AuthorizedRoots, PathRefused
from aivault_agent.scanner import TRASH_DIR, Entry, _cluster_size, _sha256, describe, identity


class OperationFailed(Exception):
    """The operation could not be done; the disk was left as it was."""


@dataclass(frozen=True)
class OperationResult:
    entry: Entry | None
    trash_id: str | None = None


@dataclass(frozen=True)
class TrashedItem:
    trash_id: str
    original_relative_path: str
    name: str
    trashed_at: str
    size_bytes: int


class LocalOperations:
    def __init__(self, roots: AuthorizedRoots) -> None:
        self.roots = roots

    # -- creating ------------------------------------------------------------

    def create_folder(self, root: Path, parent: str, name: str) -> OperationResult:
        real_root, folder = self._folder(root, parent)
        target = self._new_path(folder, name)
        target.mkdir()
        if not target.is_dir():
            raise OperationFailed("The folder was not created.")
        return OperationResult(self._describe(real_root, target))

    def create_file(self, root: Path, parent: str, name: str, content: str) -> OperationResult:
        real_root, folder = self._folder(root, parent)
        target = self._new_path(folder, name)
        data = content.encode("utf-8")
        with open(target, "xb") as handle:
            handle.write(data)
        if target.read_bytes() != data:
            raise OperationFailed("The file on disk does not match what was written.")
        return OperationResult(self._describe(real_root, target, with_hash=True))

    def copy(
        self, root: Path, relative: str, file_id: str, destination: str, new_name: str | None
    ) -> OperationResult:
        real_root, source = self._existing(root, relative, file_id)
        _, folder = self._folder(root, destination)
        target = self._new_path(folder, new_name or source.name)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
            if _sha256(source) != _sha256(target):
                target.unlink()
                raise OperationFailed("The copy did not match the original and was removed.")
        return OperationResult(self._describe(real_root, target, with_hash=not source.is_dir()))

    # -- changing ------------------------------------------------------------

    def rename(self, root: Path, relative: str, file_id: str, new_name: str) -> OperationResult:
        real_root, source = self._existing(root, relative, file_id, for_change=True)
        target = self._new_path(source.parent, new_name)
        os.rename(source, target)
        return OperationResult(self._confirm_moved(real_root, source, target, file_id))

    def move(self, root: Path, relative: str, file_id: str, destination: str) -> OperationResult:
        real_root, source = self._existing(root, relative, file_id, for_change=True)
        _, folder = self._folder(root, destination)
        if source.is_dir() and (folder == source or source in folder.parents):
            raise OperationFailed("A folder can't be moved into itself.")
        if os.stat(folder).st_dev != os.stat(source).st_dev:
            raise OperationFailed("Moving between volumes isn't supported yet.")
        target = self._new_path(folder, source.name)
        os.rename(source, target)
        return OperationResult(self._confirm_moved(real_root, source, target, file_id))

    def trash(self, root: Path, relative: str, file_id: str) -> OperationResult:
        real_root, source = self._existing(root, relative, file_id, for_change=True)
        trash_id = uuid.uuid4().hex
        slot = self._trash_dir(real_root) / trash_id
        slot.mkdir(parents=True)
        size = 0 if source.is_dir() else os.stat(source, follow_symlinks=False).st_size
        manifest = {
            "original_relative_path": source.relative_to(real_root).as_posix(),
            "name": source.name,
            "trashed_at": datetime.now(UTC).isoformat(),
            "size_bytes": size,
        }
        (slot.parent / f"{trash_id}.json").write_text(json.dumps(manifest), encoding="utf-8")
        target = slot / source.name
        os.rename(source, target)
        if source.exists() or not target.exists():
            raise OperationFailed("The item did not move to Trash.")
        return OperationResult(None, trash_id=trash_id)

    def restore(self, root: Path, trash_id: str) -> OperationResult:
        real_root = self.roots.check(root)
        item = self._trashed(real_root, trash_id)
        original = self.roots.check(real_root / item.original_relative_path, for_change=True)
        if original.exists():
            raise OperationFailed("Something already exists where this item used to be.")
        original.parent.mkdir(parents=True, exist_ok=True)
        slot = self._trash_dir(real_root) / trash_id
        os.rename(slot / item.name, original)
        if not original.exists():
            raise OperationFailed("The item was not restored.")
        slot.rmdir()
        (slot.parent / f"{trash_id}.json").unlink()
        return OperationResult(self._describe(real_root, original))

    def permanent_delete(self, root: Path, trash_id: str, *, confirm: bool) -> OperationResult:
        """Deletes for good — only something already in the agent's Trash, and
        only with explicit confirmation. Never follows links or crosses onto
        another volume while deleting."""
        real_root = self.roots.check(root)
        item = self._trashed(real_root, trash_id)
        if not confirm:
            raise OperationFailed("Permanent deletion must be confirmed.")
        slot = self._trash_dir(real_root) / trash_id
        _remove_tree(slot, os.stat(slot).st_dev)
        (slot.parent / f"{trash_id}.json").unlink()
        if slot.exists():
            raise OperationFailed(f"{item.name} could not be fully deleted.")
        return OperationResult(None, trash_id=trash_id)

    def list_trash(self, root: Path) -> list[TrashedItem]:
        trash = self._trash_dir(self.roots.check(root))
        if not trash.exists():
            return []
        return sorted(
            (self._trashed(trash.parent, manifest.stem) for manifest in trash.glob("*.json")),
            key=lambda item: item.trashed_at,
        )

    def read(self, root: Path, relative: str, file_id: str, *, max_bytes: int) -> bytes:
        _, source = self._existing(root, relative, file_id)
        if source.is_dir():
            raise OperationFailed("A folder can't be read as a file.")
        with open(source, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OperationFailed("The file is larger than the read limit.")
        return data

    def describe_path(self, root: Path, relative: str) -> Entry:
        real_root = self.roots.check(root)
        path = self.roots.check(real_root / relative)
        if not path.exists():
            raise OperationFailed(f"{relative} does not exist.")
        return self._describe(real_root, path)

    # -- helpers -------------------------------------------------------------

    def _folder(self, root: Path, relative: str) -> tuple[Path, Path]:
        real_root = self.roots.check(root)
        folder = self.roots.check(real_root / relative) if relative else real_root
        if not folder.is_dir():
            raise OperationFailed(f"{relative or 'The folder'} is not a folder.")
        if TRASH_DIR in folder.relative_to(real_root).parts:
            raise PathRefused("The agent's Trash can't be used as a destination.")
        return real_root, folder

    def _existing(
        self, root: Path, relative: str, file_id: str, *, for_change: bool = False
    ) -> tuple[Path, Path]:
        """The path the cloud means — confirmed by identity, so a file that was
        replaced or moved since the last scan is never acted on by mistake."""
        real_root = self.roots.check(root)
        path = self.roots.check(real_root / relative, for_change=for_change)
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as error:
            raise OperationFailed(f"{relative} no longer exists on disk.") from error
        if identity(info) != file_id:
            raise OperationFailed(f"{relative} is no longer the same file — rescan first.")
        if TRASH_DIR in path.relative_to(real_root).parts:
            raise PathRefused("Items in the agent's Trash can only be restored or deleted.")
        return real_root, path

    def _new_path(self, folder: Path, name: str) -> Path:
        target = self.roots.check(folder / self.roots.check_new_name(name), for_change=True)
        if os.path.lexists(target):
            raise OperationFailed(f"{name} already exists there.")
        return target

    def _confirm_moved(self, root: Path, source: Path, target: Path, file_id: str) -> Entry:
        if os.path.lexists(source) or not os.path.lexists(target):
            raise OperationFailed("The change could not be confirmed on disk.")
        entry = self._describe(root, target)
        if entry.id != file_id:
            raise OperationFailed("The file at the new location is not the one that was moved.")
        return entry

    def _describe(self, root: Path, path: Path, *, with_hash: bool = False) -> Entry:
        parent_id = identity(os.stat(path.parent))
        entry = describe(path, root, parent_id=parent_id, cluster=_cluster_size(root))
        if with_hash and not entry.is_dir:
            from dataclasses import replace

            entry = replace(entry, sha256=_sha256(path))
        return entry

    def _trash_dir(self, root: Path) -> Path:
        return root / TRASH_DIR

    def _trashed(self, root: Path, trash_id: str) -> TrashedItem:
        if not trash_id.isalnum():
            raise OperationFailed("That item is not in the agent's Trash.")
        manifest = self._trash_dir(root) / f"{trash_id}.json"
        if not manifest.exists():
            raise OperationFailed("That item is not in the agent's Trash.")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return TrashedItem(trash_id=trash_id, **data)


def _remove_tree(path: Path, device: int) -> None:
    """`rm -rf --one-file-system`, without a shell: links are removed, never
    followed, and nothing on another volume is touched."""
    info = os.stat(path, follow_symlinks=False)
    attributes = getattr(info, "st_file_attributes", 0)
    is_link = stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)
    if is_link:
        os.rmdir(path) if stat.S_ISDIR(info.st_mode) else os.unlink(path)
        return
    if not stat.S_ISDIR(info.st_mode):
        if info.st_mode & stat.S_IWRITE == 0:
            os.chmod(path, info.st_mode | stat.S_IWRITE)
        os.unlink(path)
        return
    if info.st_dev != device:
        raise OperationFailed(f"{path} is on another volume and was left alone.")
    for child in os.scandir(path):
        _remove_tree(Path(child.path), device)
    os.rmdir(path)
