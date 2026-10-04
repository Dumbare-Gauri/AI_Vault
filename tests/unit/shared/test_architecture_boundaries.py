"""Static guards for the two Phase 0 architectural invariants:

1. The Execution Engine is the only code that mutates user storage.
2. AI code can reason and recommend, but has no path to storage, credentials,
   the database, the filesystem, the shell, or arbitrary URLs.

These read source with `ast`, so a future import or call that crosses a
boundary fails the build instead of relying on review to notice. They scan
whichever source roots exist in the checkout (the service containers each
mount only part of the repo); the mutation test needs the worker sources and
skips, loudly, without them."""

import ast
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from vault_shared.storage import MUTATING_METHODS

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOTS = [
    ROOT / "apps" / "backend" / "app",
    ROOT / "apps" / "worker" / "worker",
    ROOT / "packages" / "shared" / "vault_shared",
]
EXECUTION_SERVICE = "apps/worker/worker/execution/execution_service.py"

# `create_folder` also exists on the adapter contract, so it is covered by the
# adapter-mutation guard rather than this name-based one.
CLIENT_WRITE_METHODS = frozenset(
    {
        "rename_file",
        "move_file",
        "set_trashed",
        "delete_file",
        "update_app_properties",
        "copy_file",
    }
)
GOOGLE_ADAPTER = "packages/shared/vault_shared/storage/adapters/google_drive.py"
HTTP_WRITE_MARKERS = ('"PATCH"', '"DELETE"', '"POST"', '"PUT"', "_patch(")
FILE_MIRROR_MUTATIONS = frozenset({"mark_trashed", "mark_permanently_deleted"})

OUTBOUND_HTTP_MODULES = frozenset({"requests", "httpx", "aiohttp", "urllib.request"})
OUTBOUND_HTTP_ALLOWED = frozenset(
    {
        "packages/shared/vault_shared/ai_gateway/providers/openai_compatible_completion_provider.py",
        "packages/shared/vault_shared/connectors/google_drive.py",
        "packages/shared/vault_shared/connectors/google_workspace.py",
    }
)

STORAGE_AND_EXECUTION_MODULES = (
    "vault_shared.connectors",
    "vault_shared.storage",
    "vault_shared.connector_service",
    "vault_shared.object_storage",
    "vault_shared.execution",
    "worker.execution",
    "app.application.execution_plan_service",
    "app.application.execution_job_service",
    "app.application.archive_service",
    "boto3",
)
SYSTEM_ACCESS_MODULES = ("subprocess", "shutil", "socket", "ctypes", "pty", "multiprocessing")


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if ".venv" not in path.parts and "__pycache__" not in path.parts:
            yield path


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@cache
def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _matches(module: str, forbidden: tuple[str, ...] | frozenset[str]) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in forbidden)


def _called_attribute_names(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _files_in(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for relative in relative_dirs:
        target = ROOT / relative
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(_python_files(target))
    return files


def _existing_roots() -> list[Path]:
    return [root for root in SOURCE_ROOTS if root.is_dir()]


AI_CORE_FILES = _files_in(
    "packages/shared/vault_shared/ai_gateway/boundary.py",
    "packages/shared/vault_shared/ai_gateway/recommendation.py",
    "packages/shared/vault_shared/ai_gateway/gateway.py",
    "packages/shared/vault_shared/ai_gateway/interfaces.py",
    "packages/shared/vault_shared/ai_gateway/openrouter.py",
    "packages/shared/vault_shared/ai_gateway/providers",
)
AI_CONSUMER_FILES = _files_in(
    "apps/backend/app/application/assistant",
    "apps/backend/app/application/conversation_service.py",
    "apps/backend/app/application/context_builder_service.py",
    "apps/backend/app/application/ai_status_service.py",
    "apps/worker/worker/intelligence",
)


def _receiver_calls(tree: ast.AST, methods: frozenset[str]) -> set[str]:
    """Calls of `methods` on a receiver that looks like a storage adapter
    (`adapter`, `self._storage...`, `storage_adapter`...). Narrowed by
    receiver name so `dict.copy()` and friends are not mistaken for
    `StorageAdapter.copy()`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in methods
        ):
            receiver = ast.unparse(node.func.value).lower()
            if "adapter" in receiver or "storage" in receiver:
                found.add(node.func.attr)
    return found


def _importers(*prefixes: str) -> set[str]:
    return {
        _relative(path)
        for root in _existing_roots()
        for path in _python_files(root)
        if any(_matches(module, prefixes) for module in _imported_modules(_parse(path)))
    }


class TestOnlyTheExecutionEngineMutatesStorage:
    def test_adapter_mutations_are_called_only_by_the_execution_service(self) -> None:
        if not (ROOT / EXECUTION_SERVICE).is_file():
            pytest.skip("worker sources are not present in this checkout")

        callers = {
            _relative(path)
            for root in _existing_roots()
            for path in _python_files(root)
            if "packages/shared/vault_shared/storage/" not in _relative(path)
            and _receiver_calls(_parse(path), MUTATING_METHODS)
        }

        assert callers == {EXECUTION_SERVICE}

    def test_provider_client_write_methods_are_called_only_by_the_google_adapter(self) -> None:
        callers = {
            _relative(path)
            for root in _existing_roots()
            for path in _python_files(root)
            if CLIENT_WRITE_METHODS & _called_attribute_names(_parse(path))
        }

        assert callers == {GOOGLE_ADAPTER}

    def test_file_mirror_mutations_are_only_applied_by_the_execution_service(self) -> None:
        if not (ROOT / EXECUTION_SERVICE).is_file():
            pytest.skip("worker sources are not present in this checkout")

        callers = {
            _relative(path)
            for root in _existing_roots()
            for path in _python_files(root)
            if FILE_MIRROR_MUTATIONS & _called_attribute_names(_parse(path))
        }

        assert callers == {EXECUTION_SERVICE}

    def test_every_write_method_on_the_drive_client_is_covered_by_the_guard(self) -> None:
        drive_client = ROOT / "packages/shared/vault_shared/connectors/google_drive.py"
        source = drive_client.read_text(encoding="utf-8")
        tree = ast.parse(source)
        client_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "GoogleDriveClient"
        )

        def writes(method: ast.FunctionDef) -> bool:
            body = ast.get_source_segment(source, method) or ""
            return any(marker in body for marker in HTTP_WRITE_MARKERS)

        write_methods = {
            method.name
            for method in client_class.body
            if isinstance(method, ast.FunctionDef)
            and not method.name.startswith("_")
            and writes(method)
        }

        assert write_methods == CLIENT_WRITE_METHODS | {"create_folder"}

    def test_the_contract_lists_exactly_the_mutating_operations(self) -> None:
        protocol_methods = {
            node.name
            for node in ast.walk(_parse(ROOT / "packages/shared/vault_shared/storage/adapter.py"))
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        }
        read_only = {
            "capabilities",
            "connect",
            "health",
            "disconnect",
            "write_access_problems",
            "list_containers",
            "scan",
            "get_change_cursor",
            "check_changes",
            "get_file",
            "get_permissions",
            "open_read",
            "export",
            "export_format_for",
            "is_native_document",
            "get_thumbnail",
        }

        assert protocol_methods == read_only | set(MUTATING_METHODS), (
            "A StorageAdapter method is neither listed as mutating nor as read-only — "
            "classify it so the Execution-Engine-only guard covers it."
        )

    def test_execution_code_never_depends_on_the_ai_gateway(self) -> None:
        execution_files = _files_in(
            "apps/worker/worker/execution", "packages/shared/vault_shared/execution"
        )
        if not execution_files:
            pytest.skip("execution sources are not present in this checkout")

        offenders = {
            _relative(path)
            for path in execution_files
            if any(
                _matches(module, ("vault_shared.ai_gateway",))
                for module in _imported_modules(_parse(path))
            )
        }

        assert offenders == set()


class TestTheAIHasNoPathToStorageOrTheSystem:
    def test_the_ai_files_were_found(self) -> None:
        assert AI_CORE_FILES, "AI gateway sources not found — the guards below would pass vacuously"

    @pytest.mark.parametrize("path", AI_CORE_FILES + AI_CONSUMER_FILES, ids=_relative)
    def test_ai_code_does_not_import_storage_or_execution_modules(self, path: Path) -> None:
        offenders = {
            module
            for module in _imported_modules(_parse(path))
            if _matches(module, STORAGE_AND_EXECUTION_MODULES)
        }

        assert offenders == set()

    @pytest.mark.parametrize("path", AI_CORE_FILES + AI_CONSUMER_FILES, ids=_relative)
    def test_ai_code_does_not_import_shell_or_process_access(self, path: Path) -> None:
        offenders = {
            module
            for module in _imported_modules(_parse(path))
            if _matches(module, SYSTEM_ACCESS_MODULES)
        }

        assert offenders == set()

    @pytest.mark.parametrize("path", AI_CORE_FILES, ids=_relative)
    def test_the_gateway_layer_has_no_database_or_filesystem_access(self, path: Path) -> None:
        allowed_os = {
            "packages/shared/vault_shared/ai_gateway/providers/local_embedding_provider.py"
        }
        forbidden = ("sqlalchemy", "redis", "vault_shared.db", "pathlib")
        if _relative(path) not in allowed_os:
            forbidden += ("os",)

        offenders = {
            module for module in _imported_modules(_parse(path)) if _matches(module, forbidden)
        }

        assert offenders == set()

    @pytest.mark.parametrize("path", AI_CORE_FILES + AI_CONSUMER_FILES, ids=_relative)
    def test_ai_code_never_evaluates_model_output_as_code(self, path: Path) -> None:
        tree = _parse(path)
        dangerous = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "compile", "__import__", "open"}
        }

        assert dangerous == set()

    @pytest.mark.parametrize("path", AI_CONSUMER_FILES, ids=_relative)
    def test_ai_consumers_do_not_make_their_own_http_calls(self, path: Path) -> None:
        offenders = {
            module
            for module in _imported_modules(_parse(path))
            if _matches(module, OUTBOUND_HTTP_MODULES)
        }

        assert offenders == set()


class TestOutboundNetworkAccessIsConfined:
    def test_only_connectors_and_the_ai_provider_make_http_requests(self) -> None:
        importers = {
            _relative(path)
            for root in _existing_roots()
            for path in _python_files(root)
            if any(
                _matches(module, OUTBOUND_HTTP_MODULES)
                for module in _imported_modules(_parse(path))
            )
        }

        assert importers <= OUTBOUND_HTTP_ALLOWED

    def test_the_ai_provider_talks_only_to_its_configured_base_url(self) -> None:
        provider = ROOT / (
            "packages/shared/vault_shared/ai_gateway/providers/"
            "openai_compatible_completion_provider.py"
        )
        tree = _parse(provider)
        literal_urls = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(("http://", "https://"))
        ]

        assert literal_urls == []


COMPOSITION_ROOTS = frozenset(
    {
        "apps/backend/app/presentation/dependencies/services.py",
        "apps/worker/worker/tasks/scan.py",
        "apps/worker/worker/tasks/enrichment.py",
        "apps/worker/worker/tasks/execution.py",
    }
)
DEFAULT_REGISTRY = "packages/shared/vault_shared/storage/default_registry.py"
ADAPTER_CONSUMERS = frozenset(
    {
        "apps/backend/app/application/file_service.py",
        "apps/worker/worker/execution/execution_service.py",
        "apps/worker/worker/scanner/scan_service.py",
        "apps/worker/worker/enrichment/enrichment_service.py",
    }
)
# Files allowed to name the Google Workspace provider. Each is provider setup,
# not business logic: the interactive OAuth connect flow, the provider's own
# modules, the connector model's enum, and plan creation's recorded target.
PROVIDER_NAME_ALLOWED = frozenset(
    {
        "apps/backend/app/application/connector_service.py",
        "packages/shared/vault_shared/db/models/storage_connector.py",
        "packages/shared/vault_shared/execution/plan_service.py",
        GOOGLE_ADAPTER,
        DEFAULT_REGISTRY,
        "packages/shared/vault_shared/connectors/google_drive.py",
    }
)


class TestProviderCodeIsConfinedToAdaptersAndCompositionRoots:
    def test_only_the_google_adapter_and_registry_import_the_drive_client(self) -> None:
        assert _importers("vault_shared.connectors.google_drive") <= {
            GOOGLE_ADAPTER,
            DEFAULT_REGISTRY,
        }

    def test_only_the_registry_imports_provider_adapters(self) -> None:
        assert _importers("vault_shared.storage.adapters") <= {DEFAULT_REGISTRY}

    def test_only_the_registry_imports_the_credential_source(self) -> None:
        assert _importers("vault_shared.storage.connector_credentials") <= {DEFAULT_REGISTRY}

    def test_only_composition_roots_import_the_default_registry(self) -> None:
        assert _importers("vault_shared.storage.default_registry") <= COMPOSITION_ROOTS

    def test_only_the_known_storage_consumers_ask_for_an_adapter(self) -> None:
        callers = {
            _relative(path)
            for root in _existing_roots()
            for path in _python_files(root)
            if "adapter_for" in _called_attribute_names(_parse(path))
        }

        assert callers <= ADAPTER_CONSUMERS

    def test_the_storage_contract_package_imports_no_provider(self) -> None:
        contract_modules = [
            path
            for path in _python_files(ROOT / "packages/shared/vault_shared/storage")
            if "adapters" not in path.parts
            and path.name not in {"default_registry.py", "connector_credentials.py"}
        ]
        assert contract_modules

        offenders = {
            _relative(path)
            for path in contract_modules
            if any(
                _matches(module, ("vault_shared.connectors", "requests", "boto3"))
                for module in _imported_modules(_parse(path))
            )
        }

        assert offenders == set()

    def test_no_storage_code_introduces_a_filesystem_path_dependency(self) -> None:
        storage_files = list(_python_files(ROOT / "packages/shared/vault_shared/storage"))

        offenders = {
            _relative(path)
            for path in storage_files
            if any(
                _matches(module, ("os", "pathlib", "shutil", "tempfile", "glob"))
                for module in _imported_modules(_parse(path))
            )
        }

        assert offenders == set()


class TestBusinessCodeHasNoProviderNameLogic:
    def test_only_provider_setup_code_names_the_provider(self) -> None:
        named: set[str] = set()
        for root in _existing_roots():
            for path in _python_files(root):
                for node in ast.walk(_parse(path)):
                    is_literal = isinstance(node, ast.Constant) and (
                        node.value == "google_workspace"
                        or (
                            isinstance(node.value, str)
                            and node.value.startswith("google_workspace:")
                        )
                    )
                    is_enum = isinstance(node, ast.Attribute) and node.attr == "GOOGLE_WORKSPACE"
                    if is_literal or is_enum:
                        named.add(_relative(path))

        assert named <= PROVIDER_NAME_ALLOWED, sorted(named - PROVIDER_NAME_ALLOWED)
