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
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aivault_agent.safety import AuthorizedRoots, PathRefused, misread_by_windows
from aivault_agent.scanner import (
    TRASH_DIR,
    Entry,
    _cluster_size,
    _sha256,
    describe,
    identity,
)


class OperationFailed(Exception):
    """The operation could not be done; the disk was left as it was."""


@dataclass(frozen=True)
class OperationResult:
    entry: Entry | None
    trash_id: str | None = None


# Already-compressed content gains nothing from deflate; it is stored as is.
_STORED_EXTENSIONS = frozenset(
    {
        "zip", "7z", "rar", "gz", "tgz", "bz2", "xz", "zst", "jpg", "jpeg", "png", "gif",
        "webp", "heic", "avif", "mp4", "mov", "mkv", "webm", "avi", "m4v", "mp3", "m4a",
        "aac", "ogg", "flac", "opus", "docx", "xlsx", "pptx", "pdf", "jar", "apk",
    }
)  # fmt: skip
_CHUNK = 1024 * 1024
# Kept free on the disk after an archive is written or unpacked, so archiving
# never fills the disk the user's system and apps run from.
SPACE_RESERVE_BYTES = 512 * 1024 * 1024


def _require_space(folder: Path, needed: int, *, what: str) -> None:
    free = shutil.disk_usage(folder).free
    if needed + SPACE_RESERVE_BYTES > free:
        raise OperationFailed(
            f"Not enough free space on this disk to {what}: it needs about "
            f"{_megabytes(needed)} and {_megabytes(max(free - SPACE_RESERVE_BYTES, 0))} is "
            "available. Free up space or choose fewer files."
        )


def _megabytes(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"


@dataclass(frozen=True)
class ArchiveEntry:
    file_id: str
    arcname: str
    size_bytes: int
    compressed_bytes: int
    stored: bool


@dataclass
class ArchiveResult:
    entry: Entry
    entries: list[ArchiveEntry] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


@dataclass
class ExtractResult:
    folder: Entry
    created: list[Entry] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


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

    # -- archives -----------------------------------------------------------

    def create_archive(
        self, root: Path, folder: str, name: str, items: list[tuple[str, str, str]]
    ) -> ArchiveResult:
        """Zips `items` - `(relative_path, file_id, name_in_archive)` - into
        a new archive in `folder`. It is written under a temporary name
        first; every entry is then read back and checked against its CRC,
        and only a sound archive is moved into place. A broken one is
        removed and nothing else on disk changes. Never overwrites."""
        real_root, destination = self._folder(root, folder)
        if not name.lower().endswith(".zip"):
            raise OperationFailed("An archive's name must end in .zip.")
        target = self._new_path(destination, name)
        partial = self._new_path(destination, f"{name}.partial")
        _require_space(
            destination, self._total_size(real_root, items), what="write this archive"
        )
        entries: list[ArchiveEntry] = []
        skipped: list[dict] = []
        used: set[str] = set()
        try:
            with zipfile.ZipFile(partial, "w", compresslevel=9) as archive:
                for relative, file_id, arcname in items:
                    added = self._add_to_archive(archive, root, relative, file_id, arcname, used)
                    if isinstance(added, ArchiveEntry):
                        entries.append(added)
                    else:
                        skipped.append({"file_id": file_id, "reason": added})
            if not entries:
                raise OperationFailed("None of the selected files could be added to the archive.")
            with zipfile.ZipFile(partial) as check:
                broken = check.testzip()
                if broken is not None or len(check.infolist()) != len(entries):
                    raise OperationFailed(f"The archive failed its integrity check ({broken}).")
            os.rename(partial, target)
        except BaseException:
            if os.path.lexists(partial):
                partial.unlink()
            raise
        return ArchiveResult(
            entry=self._describe(real_root, target, with_hash=True),
            entries=entries,
            skipped=skipped,
        )

    @staticmethod
    def _total_size(real_root: Path, items: list[tuple[str, str, str]]) -> int:
        """At most what the archive can take up: deflate only shrinks."""
        total = 0
        for relative, _, _ in items:
            try:
                total += os.stat(real_root / relative, follow_symlinks=False).st_size
            except OSError:
                continue
        return total

    def _add_to_archive(
        self,
        archive: zipfile.ZipFile,
        root: Path,
        relative: str,
        file_id: str,
        arcname: str,
        used: set[str],
    ) -> ArchiveEntry | str:
        """The entry written, or why this file was left out."""
        try:
            _, source = self._existing(root, relative, file_id)
        except (OperationFailed, PathRefused) as error:
            return str(error)
        if source.is_dir():
            return "A folder is archived through its files."
        clean = _safe_arcname(arcname)
        if clean is None or clean in used:
            return f"Unusable name inside the archive: {arcname}"
        used.add(clean)
        stored = clean.rsplit(".", 1)[-1].lower() in _STORED_EXTENSIONS
        info = zipfile.ZipInfo.from_file(source, clean)
        info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        with open(source, "rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
            while chunk := reader.read(_CHUNK):
                writer.write(chunk)
        written = archive.getinfo(clean)
        return ArchiveEntry(
            file_id=file_id,
            arcname=clean,
            size_bytes=written.file_size,
            compressed_bytes=written.compress_size,
            stored=stored,
        )

    def archive_contents(self, root: Path, relative: str, file_id: str) -> dict:
        """What an archive holds, read from its index — nothing is unpacked
        or written. Entries that would escape their folder are flagged."""
        _, path = self._existing(root, relative, file_id)
        try:
            with zipfile.ZipFile(path) as archive:
                return describe_members(archive.infolist(), safe_name=_safe_arcname)
        except zipfile.BadZipFile as error:
            raise OperationFailed("That file isn't a readable zip archive.") from error

    def extract_archive(
        self, root: Path, relative: str, file_id: str, folder: str
    ) -> ExtractResult:
        """Unpacks an archive into a new folder, named after the archive,
        inside `folder`. Every name is checked first: nothing absolute, no
        `..`, nothing Windows would read as another name - such an entry is
        skipped, never written outside the new folder. Never overwrites."""
        real_root, archive_path = self._existing(root, relative, file_id)
        _, destination = self._folder(root, folder)
        stem = archive_path.name
        if stem.lower().endswith(".zip"):
            stem = stem[:-4]
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _require_space(
                    destination,
                    sum(member.file_size for member in archive.infolist()),
                    what="unpack this archive",
                )
        except zipfile.BadZipFile as error:
            raise OperationFailed("That file isn't a readable zip archive.") from error
        target = self._unused_folder(destination, stem)
        target.mkdir()
        result = ExtractResult(folder=self._describe(real_root, target))
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    self._extract_member(archive, member, real_root, target, result)
        except zipfile.BadZipFile as error:
            raise OperationFailed("That file isn't a readable zip archive.") from error
        return result

    def _extract_member(
        self,
        archive: zipfile.ZipFile,
        member: zipfile.ZipInfo,
        real_root: Path,
        target: Path,
        result: ExtractResult,
    ) -> None:
        clean = _safe_arcname(member.filename.rstrip("/"))
        if clean is None:
            result.skipped.append({"name": member.filename, "reason": "Unsafe path"})
            return
        parts = clean.split("/")
        directory = target
        for part in parts[:-1] if not member.is_dir() else parts:
            directory = self.roots.check(directory / part, for_change=True)
            if not directory.exists():
                directory.mkdir()
                result.created.append(self._describe(real_root, directory))
        if member.is_dir():
            return
        out = self.roots.check(directory / parts[-1], for_change=True)
        if os.path.lexists(out):
            result.skipped.append({"name": clean, "reason": "Already exists"})
            return
        with archive.open(member) as reader, open(out, "xb") as writer:
            shutil.copyfileobj(reader, writer, _CHUNK)
        if os.stat(out).st_size != member.file_size:
            raise OperationFailed(f"{clean} did not extract completely.")
        result.created.append(self._describe(real_root, out))

    def _unused_folder(self, folder: Path, name: str) -> Path:
        candidate = self._new_path_name(folder, name)
        number = 2
        while os.path.lexists(candidate):
            candidate = self._new_path_name(folder, f"{name} ({number})")
            number += 1
        return candidate

    def _new_path_name(self, folder: Path, name: str) -> Path:
        return self.roots.check(folder / self.roots.check_new_name(name), for_change=True)

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


MAX_LISTED_ENTRIES = 5000


def describe_members(members: list[zipfile.ZipInfo], *, safe_name) -> dict:  # noqa: ANN001
    """An archive's index as plain data: every entry is counted, the first
    `MAX_LISTED_ENTRIES` are listed, and unsafe paths are flagged."""
    entries = [
        {
            "name": member.filename,
            "size_bytes": member.file_size,
            "compressed_bytes": member.compress_size,
            "is_folder": member.is_dir(),
            "safe": safe_name(member.filename.rstrip("/")) is not None,
        }
        for member in members
    ]
    return {
        "entries": entries[:MAX_LISTED_ENTRIES],
        "entry_count": len(entries),
        "file_count": sum(1 for entry in entries if not entry["is_folder"]),
        "total_bytes": sum(entry["size_bytes"] for entry in entries),
        "unsafe_count": sum(1 for entry in entries if not entry["safe"]),
    }


def _safe_arcname(name: str) -> str | None:
    """A name inside an archive that stays inside wherever it is unpacked:
    relative, forward slashes, no `.`/`..` parts, no drive letters, nothing
    Windows would read as another name. None if it can't be made so."""
    if name.startswith(("/", "\\")):
        return None
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ":" in parts[0]:
        return None
    for part in parts:
        if part == ".." or misread_by_windows(part) or any(ord(ch) < 32 for ch in part):
            return None
    return "/".join(parts)
