"""Which files AI Vault refuses to remove or rearrange without the user's
explicit say-so — decided here, from the index, never by a model.

A software project only works as a whole: an empty `__init__.py`, a lockfile
or a `tsconfig.json` looks redundant on its own and breaks the project when
it goes. So source code, project configuration, anything inside a detected
project, and installed program/driver files are *protected*: a bulk cleanup,
a duplicate cleanup, an AI-proposed organization or a recommendation never
trashes, deletes, moves or renames them. Only a user acting on files they
picked themselves, and acknowledging that they're protected, can.

The same check runs twice: when a plan is created (so the preview is
exact) and again in the Execution Engine right before each mutation (so a
plan built by any other path, or a file that became protected since, is
still refused).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from vault_shared.db.models import ExecutionActionType, File
from vault_shared.db.repositories import FileRepository, FolderRepository

_SOURCE_EXTENSIONS = frozenset(
    {
        "py", "pyi", "pyx", "pxd", "ipynb",
        "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "vue", "svelte",
        "c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx", "m", "mm",
        "cs", "java", "kt", "kts", "scala", "groovy", "go", "rs", "swift", "dart",
        "php", "rb", "pl", "lua", "r", "ex", "exs", "erl", "hs", "clj", "fs",
        "sh", "bash", "zsh", "fish", "ps1", "psm1", "bat", "cmd",
        "sql", "graphql", "proto",
        "tf", "tfvars", "hcl", "bicep",
    }
)  # fmt: skip
_CONFIG_EXTENSIONS = frozenset(
    {
        "json", "jsonc", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
        "lock", "gradle", "properties", "csproj", "fsproj", "vbproj", "sln",
        "vcxproj", "cmake", "mk", "editorconfig", "gitignore", "gitattributes",
        "dockerignore", "npmrc", "nvmrc", "gitkeep", "keep",
        "pem", "key", "crt", "cer", "p12", "pfx",
    }
)  # fmt: skip
_PROGRAM_EXTENSIONS = frozenset(
    {
        "exe", "dll", "sys", "inf", "cat", "msi", "msix", "msixbundle", "appx",
        "appxbundle", "drv", "ocx", "cpl", "mui", "so", "dylib", "ko", "pdb",
    }
)  # fmt: skip
_PROTECTED_NAMES = frozenset(
    {
        "dockerfile", "makefile", "procfile", "jenkinsfile", "vagrantfile",
        "gemfile", "rakefile", "codeowners", "license", "license.md", "license.txt",
        "readme", "readme.md", "readme.rst", "readme.txt", "py.typed",
    }
)  # fmt: skip
# A folder holding one of these is the root of a project.
PROJECT_MARKER_FILES = frozenset(
    {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "pipfile", "poetry.lock", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
        "build.gradle.kts", "settings.gradle", "composer.json", "gemfile", "cmakelists.txt",
        "pubspec.yaml", "mix.exs", "deno.json", "tsconfig.json", "pnpm-workspace.yaml",
        "turbo.json", "alembic.ini", "manage.py",
    }
)  # fmt: skip
PROJECT_MARKER_SUFFIXES = (".sln", ".csproj", ".xcodeproj")
PROJECT_MARKER_FOLDERS = frozenset({".git", ".hg", ".svn"})
_PROTECTED_SEGMENTS = frozenset(
    {".git", ".github", ".gitlab", ".circleci", ".hg", ".svn", "migrations", "alembic"}
)

# Actions that take a file away from where its project expects it.
_REMOVING = frozenset(
    {
        ExecutionActionType.ARCHIVE,
        ExecutionActionType.REMOVE_DUPLICATE,
        ExecutionActionType.PERMANENT_DELETE,
    }
)
_RELOCATING = frozenset(
    {ExecutionActionType.RENAME, ExecutionActionType.MOVE_FILE, ExecutionActionType.MOVE_FOLDER}
)


def guards(action_type: str, planned_change: dict | None = None) -> bool:
    """Whether protection applies to this action at all. Restoring from
    Trash, zipping a copy that keeps the originals, and tagging metadata
    never take a file away from where it is."""
    if action_type == ExecutionActionType.CREATE_ARCHIVE:
        return bool((planned_change or {}).get("remove_originals"))
    return action_type in _REMOVING or action_type in _RELOCATING


def _extension(name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    if not dot:
        return ""
    # ".env", ".gitignore": the whole name is the "extension".
    return (extension if stem else name.lstrip(".")).lower()


def _parent(path: str) -> str:
    parent = path.rstrip("/").rpartition("/")[0]
    return parent or "/"


def _within(path: str, folder: str) -> bool:
    folder = folder.rstrip("/")
    return folder == "" or path.lower().startswith(folder.lower() + "/")


def kind_reasons(name: str, path: str) -> list[str]:
    """What a file's own name and location say, before looking at its
    neighbours."""
    reasons: list[str] = []
    lowered = name.lower()
    extension = _extension(name)
    if extension in _SOURCE_EXTENSIONS:
        reasons.append(f"Source code (.{extension})")
    elif lowered in _PROTECTED_NAMES or lowered in PROJECT_MARKER_FILES:
        reasons.append(f"Project file ({name})")
    elif lowered.startswith(".env") or extension in _CONFIG_EXTENSIONS:
        reasons.append(f"Configuration file ({name})")
    elif extension in _PROGRAM_EXTENSIONS:
        reasons.append(f"Program or driver file (.{extension})")
    segments = {segment.lower() for segment in path.split("/")[:-1]}
    if protected := sorted(segments & _PROTECTED_SEGMENTS):
        reasons.append(f"Inside {protected[0]}/, which belongs to a project")
    return reasons


@dataclass(frozen=True)
class ProtectedFile:
    file: File
    reasons: list[str]


class AssetProtection:
    """Looks up the evidence a file's neighbours give — manifests and
    version-control folders marking a project root — once per storage, then
    answers per file."""

    def __init__(self, db: Session) -> None:
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._roots: dict[uuid.UUID, list[str]] = {}

    def reasons(self, file: File) -> list[str]:
        reasons = kind_reasons(file.name, file.path or f"/{file.name}")
        root = self._project_root(file)
        if root is not None:
            reasons.append(f"Part of the software project in {root}")
        return reasons

    def protected(self, files: list[File]) -> list[ProtectedFile]:
        self._load({file.storage_source_id for file in files})
        found = [ProtectedFile(file, self.reasons(file)) for file in files]
        return [entry for entry in found if entry.reasons]

    def split(self, files: list[File]) -> tuple[list[File], list[ProtectedFile]]:
        """`(free, protected)` — the order of `files` is kept in both."""
        protected = self.protected(files)
        ids = {entry.file.id for entry in protected}
        return [file for file in files if file.id not in ids], protected

    def _project_root(self, file: File) -> str | None:
        self._load({file.storage_source_id})
        path = file.path or f"/{file.name}"
        roots = [root for root in self._roots[file.storage_source_id] if _within(path, root)]
        return max(roots, key=len) if roots else None

    def _load(self, source_ids: set[uuid.UUID]) -> None:
        missing = [source_id for source_id in source_ids if source_id not in self._roots]
        if not missing:
            return
        roots: dict[uuid.UUID, set[str]] = defaultdict(set)
        for source_id, path in self._files.list_paths_marking_projects(
            missing, names=PROJECT_MARKER_FILES, suffixes=PROJECT_MARKER_SUFFIXES
        ):
            roots[source_id].add(_parent(path))
        for source_id, path in self._folders.list_paths_named(
            missing, names=PROJECT_MARKER_FOLDERS
        ):
            roots[source_id].add(_parent(path))
        for source_id in missing:
            self._roots[source_id] = sorted(roots.get(source_id, set()))


def describe(protected: list[ProtectedFile], *, limit: int = 5) -> str:
    """One sentence naming the protected files, for a refusal message."""
    names = ", ".join(f"{entry.file.name} ({entry.reasons[0]})" for entry in protected[:limit])
    more = f" and {len(protected) - limit} more" if len(protected) > limit else ""
    return f"{names}{more}"
