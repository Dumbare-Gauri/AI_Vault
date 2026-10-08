"""The agent's whole command surface: a fixed table of typed operations.

A command is `{"op": name, "params": {...}}`. Anything not in the table is
refused, every parameter is checked for name and type, and the reply is
always a plain result — nothing a command contains is ever run as code or
passed to a shell. Files are addressed by their stable identity (device and
file id), which survives renames, moves and the agent's Trash.
"""

import base64
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aivault_agent.operations import LocalOperations, OperationFailed
from aivault_agent.safety import AuthorizedRoots, PathRefused, path_key
from aivault_agent.scanner import (
    _FILE_ATTRIBUTE_HIDDEN,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    TRASH_DIR,
    Entry,
    ScanResult,
    identity,
    scan,
)
from aivault_agent.settings import RootsFile
from aivault_agent.volumes import list_volumes

_DEFAULT_HASH_MAX_BYTES = 50 * 1024 * 1024
_MAX_READ_BYTES = 20 * 1024 * 1024
_MAX_BROWSE_FOLDERS = 1000


class _NotFound(Exception):
    pass


@dataclass
class Place:
    root: Path
    relative_path: str
    trash_id: str | None = None


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


class AgentService:
    def __init__(
        self,
        roots: list[Path],
        *,
        roots_file: RootsFile | None = None,
        hash_max_bytes: int = _DEFAULT_HASH_MAX_BYTES,
    ) -> None:
        self.authorized = AuthorizedRoots(roots)
        self.roots_file = roots_file
        # Every folder the user gave the agent, including ones on a drive
        # that isn't plugged in right now.
        self.remembered = roots_file.load() if roots_file else []
        self.recheck_missing_roots()
        self.ops = LocalOperations(self.authorized)
        self.hash_max_bytes = hash_max_bytes
        self.index: dict[str, Place] = {}
        self.last_scan: ScanResult | None = None
        # Scans walk the disk without holding this, so a long scan never
        # holds up a command; everything that reads or changes the index does.
        self.lock = threading.RLock()
        self._commands: dict[str, tuple[dict[str, type], dict[str, type], Callable[..., dict]]] = {
            "volumes": ({}, {}, self._volumes),
            "roots": ({}, {}, self._roots),
            "browse": ({"path": str}, {}, self._browse),
            "add_root": ({"path": str}, {}, self._add_root),
            "remove_root": ({"path": str}, {}, self._remove_root),
            "scan": ({"root": str}, {}, self._scan),
            "get": ({"id": str}, {}, self._get),
            "create_folder": ({"name": str}, {"root": str, "parent_id": str}, self._create_folder),
            "create_file": (
                {"name": str, "content": str},
                {"root": str, "parent_id": str},
                self._create_file,
            ),
            "rename": ({"id": str, "new_name": str}, {}, self._rename),
            "move": ({"id": str, "parent_id": str}, {}, self._move),
            "copy": ({"id": str}, {"parent_id": str, "new_name": str}, self._copy),
            "trash": ({"id": str}, {}, self._trash),
            "restore": ({"id": str}, {}, self._restore),
            "permanent_delete": ({"id": str, "confirm": bool}, {}, self._permanent_delete),
            "read": ({"id": str}, {"max_bytes": int}, self._read),
            "list_trash": ({"root": str}, {}, self._list_trash),
            "trash_contents": ({}, {}, self._trash_contents),
            "empty_trash": ({"confirm": bool}, {}, self._empty_trash),
        }

    def handle(self, command: dict) -> dict:
        op = command.get("op")
        params = command.get("params") or {}
        spec = self._commands.get(op) if isinstance(op, str) else None
        if spec is None:
            return {"ok": False, "kind": "refused", "error": f"Unknown operation: {op}"}
        required, optional, handler = spec
        problem = _check_params(params, required, optional)
        if problem:
            return {"ok": False, "kind": "refused", "error": problem}
        try:
            if op in ("scan", "browse", "volumes"):
                return {"ok": True, **handler(**params)}
            with self.lock:
                return {"ok": True, **handler(**params)}
        except PathRefused as error:
            return {"ok": False, "kind": "refused", "error": str(error)}
        except _NotFound as error:
            return {"ok": False, "kind": "not_found", "error": str(error)}
        except OperationFailed as error:
            return {"ok": False, "kind": "failed", "error": str(error)}
        except OSError as error:
            return {"ok": False, "kind": "failed", "error": error.strerror or str(error)}

    # -- reading ---------------------------------------------------------------

    def _volumes(self) -> dict:
        return {"volumes": [asdict(volume) for volume in list_volumes()]}

    def _roots(self) -> dict:
        return {"roots": [str(root) for root in self.authorized.roots]}

    def missing_roots(self) -> list[str]:
        """Remembered folders that aren't available now — an unplugged drive."""
        return [root for root in self.remembered if not self.authorized.covers(root)]

    def recheck_missing_roots(self) -> list[Path]:
        """Takes back any remembered folder whose drive has returned."""
        returned = []
        for root in self.missing_roots():
            if os.path.isdir(root):
                try:
                    returned.append(self.authorized.add(Path(root)))
                except PathRefused:
                    continue
        return returned

    def _browse(self, path: str) -> dict:
        """The folders inside one folder, so the user can pick what to give
        the agent. Only folder names are listed — never files or contents —
        and links are left out rather than followed."""
        folder = Path(path)
        if not folder.is_absolute():
            raise PathRefused("Only absolute paths are accepted.")
        folder = Path(os.path.normpath(folder))
        if path_key(os.path.realpath(folder)) != path_key(folder):
            raise PathRefused(f"{folder} is reached through a link")
        if system := self.authorized.system_tree(folder):
            raise PathRefused(f"{folder} is part of the system under {system}")
        folders: list[dict[str, Any]] = []
        with os.scandir(folder) as children:
            for child in children:
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                attributes = getattr(info, "st_file_attributes", 0)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_HIDDEN)
                    or child.name.startswith((".", "$"))
                ):
                    continue
                child_path = Path(child.path)
                reason = self.authorized.refusal(child_path)
                folders.append(
                    {
                        "name": child.name,
                        "path": str(child_path),
                        "can_add": reason is None,
                        "reason": reason,
                        "added": self.authorized.covers(child_path),
                    }
                )
        folders.sort(key=lambda item: item["name"].lower())
        return {
            "path": str(folder),
            "parent": str(folder.parent) if folder.parent != folder else None,
            "can_add": self.authorized.refusal(folder) is None,
            "reason": self.authorized.refusal(folder),
            "folders": folders[:_MAX_BROWSE_FOLDERS],
            "truncated": len(folders) > _MAX_BROWSE_FOLDERS,
        }

    def _add_root(self, path: str) -> dict:
        root = self.authorized.add(Path(path))
        if not any(path_key(r) == path_key(root) for r in self.remembered):
            self.remembered.append(str(root))
        self._save_roots()
        return {"root": str(root), "roots": [str(r) for r in self.authorized.roots]}

    def _remove_root(self, path: str) -> dict:
        """Stops the agent working in a folder. Nothing on disk changes."""
        key = path_key(Path(path).resolve())
        remembered = [r for r in self.remembered if path_key(Path(r).resolve()) != key]
        if self.authorized.covers(path):
            root = self.authorized.remove(path)
            self.index = {fid: p for fid, p in self.index.items() if p.root != root}
        elif len(remembered) == len(self.remembered):
            raise PathRefused(f"{path} isn't one of the agent's folders")
        self.remembered = remembered
        self._save_roots()
        return {"roots": [str(r) for r in self.authorized.roots]}

    def _save_roots(self) -> None:
        if self.roots_file is not None:
            self.roots_file.save(self.remembered)

    def scan_root(self, root: str) -> tuple[dict, ScanResult]:
        """Walks one authorized folder and refreshes the index from it."""
        result = scan(Path(root), self.authorized, hash_max_bytes=self.hash_max_bytes)
        root_id = identity(os.stat(result.root))
        with self.lock:
            # The folder itself, so "move into this folder's top level" can name it.
            self.index[root_id] = Place(result.root, "")
            for entry in result.entries:
                self.index[entry.id] = Place(result.root, entry.relative_path)
            for item in self.ops.list_trash(result.root):
                slot = result.root / TRASH_DIR / item.trash_id / item.name
                if os.path.lexists(slot):
                    file_id = identity(os.stat(slot, follow_symlinks=False))
                    self.index[file_id] = Place(
                        result.root, item.original_relative_path, item.trash_id
                    )
            self.last_scan = result
        return self._summary(result, root_id), result

    def _scan(self, root: str) -> dict:
        return self.scan_root(root)[0]

    def _summary(self, result: ScanResult, root_id: str) -> dict:
        return {
            "root": str(result.root),
            "root_id": root_id,
            "entries": len(result.entries),
            "apparent_bytes": result.apparent_bytes,
            "allocated_bytes": result.allocated_bytes,
            "errors": [{"path": path, "error": error} for path, error in result.errors],
        }

    def _get(self, id: str) -> dict:  # noqa: A002 - the protocol's name
        place = self._place(id)
        return {"file": self._file_dict(id, place)}

    def _read(self, id: str, max_bytes: int = _MAX_READ_BYTES) -> dict:  # noqa: A002
        place = self._place(id)
        data = self.ops.read(
            place.root, place.relative_path, id, max_bytes=min(max_bytes, _MAX_READ_BYTES)
        )
        return {"content_base64": base64.b64encode(data).decode("ascii"), "size_bytes": len(data)}

    def _list_trash(self, root: str) -> dict:
        return {"items": [asdict(item) for item in self.ops.list_trash(Path(root))]}

    def _trash_contents(self) -> dict:
        """Everything in the agent's Trash, in every authorized folder — what
        `empty_trash` would delete."""
        for root in self.authorized.roots:
            self._scan(str(root))
        files = [
            self._file_dict(file_id, place)
            for file_id, place in self.index.items()
            if place.trash_id is not None
        ]
        return {"files": sorted(files, key=lambda item: item["name"])}

    # -- changing --------------------------------------------------------------

    def _create_folder(
        self, name: str, root: str | None = None, parent_id: str | None = None
    ) -> dict:
        base, parent = self._parent(root, parent_id)
        result = self.ops.create_folder(base, parent, name)
        return self._remember(base, result.entry)

    def _create_file(
        self, name: str, content: str, root: str | None = None, parent_id: str | None = None
    ) -> dict:
        base, parent = self._parent(root, parent_id)
        result = self.ops.create_file(base, parent, name, content)
        return self._remember(base, result.entry)

    def _rename(self, id: str, new_name: str) -> dict:  # noqa: A002
        place = self._active(id)
        result = self.ops.rename(place.root, place.relative_path, id, new_name)
        return self._remember(place.root, result.entry)

    def _move(self, id: str, parent_id: str) -> dict:  # noqa: A002
        place = self._active(id)
        destination = self._active(parent_id)
        if destination.root != place.root:
            raise OperationFailed("Moving between authorized folders isn't supported yet.")
        result = self.ops.move(place.root, place.relative_path, id, destination.relative_path)
        return self._remember(place.root, result.entry)

    def _copy(self, id: str, parent_id: str | None = None, new_name: str | None = None) -> dict:  # noqa: A002
        place = self._active(id)
        destination = (
            self._active(parent_id).relative_path
            if parent_id
            else str(Path(place.relative_path).parent)
        )
        destination = "" if destination == "." else destination
        result = self.ops.copy(place.root, place.relative_path, id, destination, new_name)
        return self._remember(place.root, result.entry)

    def _trash(self, id: str) -> dict:  # noqa: A002
        place = self._active(id)
        result = self.ops.trash(place.root, place.relative_path, id)
        self.index[id] = Place(place.root, place.relative_path, result.trash_id)
        return {"file": self._file_dict(id, self.index[id])}

    def _restore(self, id: str) -> dict:  # noqa: A002
        place = self._place(id)
        if place.trash_id is None:
            return {"file": self._file_dict(id, place)}
        result = self.ops.restore(place.root, place.trash_id)
        return self._remember(place.root, result.entry)

    def _permanent_delete(self, id: str, confirm: bool) -> dict:  # noqa: A002
        place = self._place(id)
        if place.trash_id is None:
            raise OperationFailed("Only items in the agent's Trash can be deleted permanently.")
        self.ops.permanent_delete(place.root, place.trash_id, confirm=confirm)
        del self.index[id]
        return {"deleted_id": id}

    def _empty_trash(self, confirm: bool) -> dict:
        """Deletes everything in the agent's Trash for good — only with
        explicit confirmation."""
        if not confirm:
            raise OperationFailed("Emptying the Trash must be confirmed.")
        deleted = 0
        for root in self.authorized.roots:
            for item in self.ops.list_trash(root):
                self.ops.permanent_delete(root, item.trash_id, confirm=True)
                deleted += 1
        self.index = {fid: place for fid, place in self.index.items() if place.trash_id is None}
        return {"deleted_count": deleted}

    # -- helpers ---------------------------------------------------------------

    def _place(self, file_id: str) -> Place:
        place = self.index.get(file_id)
        if place is None or not self._still_there(file_id, place):
            roots = [place.root] if place is not None else list(self.authorized.roots)
            for root in roots:
                self._scan(str(root))
            place = self.index.get(file_id)
            if place is None or not self._still_there(file_id, place):
                self.index.pop(file_id, None)
                raise _NotFound("That file no longer exists on disk.")
        return place

    def _active(self, file_id: str) -> Place:
        place = self._place(file_id)
        if place.trash_id is not None:
            raise OperationFailed("That item is in Trash — restore it first.")
        return place

    def _location(self, place: Place) -> Path:
        if place.trash_id is None:
            return place.root / place.relative_path
        return place.root / TRASH_DIR / place.trash_id / Path(place.relative_path).name

    def _still_there(self, file_id: str, place: Place) -> bool:
        try:
            return identity(os.stat(self._location(place), follow_symlinks=False)) == file_id
        except OSError:
            return False

    def _parent(self, root: str | None, parent_id: str | None) -> tuple[Path, str]:
        if parent_id:
            place = self._active(parent_id)
            return place.root, place.relative_path
        if root is None:
            raise OperationFailed("Say where to create it: a root folder or a parent folder.")
        return self.authorized.check(Path(root)), ""

    def _remember(self, root: Path, entry: Entry | None) -> dict:
        if entry is None:
            return {}
        self.index[entry.id] = Place(root, entry.relative_path)
        return {"file": self._file_dict(entry.id, self.index[entry.id], entry)}

    def _file_dict(self, file_id: str, place: Place, entry: Entry | None = None) -> dict:
        if entry is None:
            path = self._location(place)
            from aivault_agent.scanner import _cluster_size, describe

            parent_id = identity(os.stat(path.parent))
            entry = describe(
                path, path.parent, parent_id=parent_id, cluster=_cluster_size(place.root)
            )
        parent = place.root / place.relative_path
        data = {key: _iso(value) for key, value in asdict(entry).items()}
        data.update(
            id=file_id,
            root=str(place.root),
            relative_path=place.relative_path,
            trashed=place.trash_id is not None,
            parent_id=identity(os.stat(parent.parent)) if parent.parent.exists() else None,
        )
        return data


def _check_params(params: Any, required: dict[str, type], optional: dict[str, type]) -> str | None:
    if not isinstance(params, dict):
        return "Parameters must be an object."
    unexpected = set(params) - set(required) - set(optional)
    if unexpected:
        return f"Unexpected parameters: {', '.join(sorted(unexpected))}"
    missing = set(required) - set(params)
    if missing:
        return f"Missing parameters: {', '.join(sorted(missing))}"
    for name, value in params.items():
        expected = required.get(name) or optional[name]
        if value is not None and (type(value) is not expected):
            return f"{name} must be a {expected.__name__}."
    return None
