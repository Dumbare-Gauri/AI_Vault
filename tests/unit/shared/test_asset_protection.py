"""What a file's own name and location say about whether AI Vault may
remove or rearrange it without the user's explicit say-so."""

import pytest

from vault_shared.db.models import ExecutionActionType
from vault_shared.protection import guards, kind_reasons


@pytest.mark.parametrize(
    "path",
    [
        "/AI_Vault/apps/backend/app/__init__.py",
        "/site/src/index.tsx",
        "/infra/main.tf",
        "/db/schema.sql",
        "/tools/deploy.ps1",
    ],
)
def test_source_code_is_protected_wherever_it_is(path: str) -> None:
    name = path.rsplit("/", 1)[-1]
    assert kind_reasons(name, path)


@pytest.mark.parametrize(
    "name",
    [
        "package.json",
        "pnpm-lock.yaml",
        ".env",
        ".env.production",
        "Dockerfile",
        "py.typed",
        "tsconfig.json",
        ".gitkeep",
        "server.pem",
        ".terraform.lock.hcl",
    ],
)
def test_project_and_configuration_files_are_protected(name: str) -> None:
    assert kind_reasons(name, f"/project/{name}")


@pytest.mark.parametrize("name", ["driver.inf", "nvlddmkm.sys", "setup.exe", "helper.dll"])
def test_program_and_driver_files_are_protected(name: str) -> None:
    assert kind_reasons(name, f"/Drivers/Display/{name}")


def test_anything_inside_a_git_folder_is_protected() -> None:
    assert kind_reasons("HEAD", "/repo/.git/HEAD")


def test_a_migration_is_protected_even_without_a_code_extension() -> None:
    assert kind_reasons("0001_initial.txt", "/app/migrations/0001_initial.txt")


@pytest.mark.parametrize(
    "name", ["holiday.jpg", "Invoice March.pdf", "notes.docx", "budget.xlsx", "clip.mp4"]
)
def test_ordinary_documents_and_media_are_not_protected_by_name(name: str) -> None:
    assert kind_reasons(name, f"/Documents/{name}") == []


def test_an_old_or_temporary_looking_name_is_not_by_itself_a_reason_either_way() -> None:
    assert kind_reasons("copy of report (2).tmp", "/Downloads/copy of report (2).tmp") == []


@pytest.mark.parametrize(
    "action_type",
    [
        ExecutionActionType.ARCHIVE,
        ExecutionActionType.REMOVE_DUPLICATE,
        ExecutionActionType.PERMANENT_DELETE,
        ExecutionActionType.MOVE_FILE,
        ExecutionActionType.RENAME,
    ],
)
def test_removing_and_relocating_are_guarded(action_type: str) -> None:
    assert guards(action_type)


@pytest.mark.parametrize(
    "action_type", [ExecutionActionType.RESTORE, ExecutionActionType.UPDATE_METADATA]
)
def test_restoring_and_tagging_are_never_blocked(action_type: str) -> None:
    assert not guards(action_type)


def test_an_archive_is_guarded_only_when_it_removes_the_originals() -> None:
    assert not guards(ExecutionActionType.CREATE_ARCHIVE, {"remove_originals": False})
    assert guards(ExecutionActionType.CREATE_ARCHIVE, {"remove_originals": True})
