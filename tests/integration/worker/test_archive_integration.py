"""Google Drive archives through the Execution Engine, against a fake Drive:
nested folder structure kept inside the zip, the archive shown in Files at
once, and extraction recreating folders without writing outside its own
folder. A fake Drive proves the engine's behavior, not Google's."""

import io
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_execution_service_integration import (
    _drive_file,
    _FakeGoogleDriveClient,
    _FakeGoogleWorkspaceOAuthClient,
    _FakeObjectStorageClient,
    _provision_connector,
    _provision_file,
    _provision_job,
    _provision_plan,
    _provision_user,
    requires_infra,
)

from vault_shared.db.models import ExecutionActionType, File, Folder
from vault_shared.db.repositories import (
    ExecutionStepRepository,
    FileRepository,
    FolderRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
from vault_shared.storage.default_registry import build_storage_registry
from worker.execution.execution_service import ExecutionService


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _Drive(_FakeGoogleDriveClient):
    """Hands back the real bytes of anything uploaded to it."""

    def download_file(self, *, access_token: str, file_id: str) -> bytes:
        if file_id in self.uploads:
            return self.uploads[file_id]
        return super().download_file(access_token=access_token, file_id=file_id)


def _engine(db: Session, drive: _Drive) -> ExecutionService:
    return ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )


def _archive_step(
    db: Session, *, plan_id: uuid.UUID, file_id: uuid.UUID, path: str, order: int, **chosen
):
    ExecutionStepRepository(db).create(
        execution_plan_id=plan_id,
        step_order=order,
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        target_file_id=file_id,
        pre_state={},
        planned_change={"action": "create_archive", "archive_path": path, **chosen},
    )
    db.commit()


def _archive(db: Session) -> tuple[_Drive, object, File]:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    a = _provision_file(db, connector_id=connector.id, name="notes.txt", provider_file_id="f-a")
    b = _provision_file(db, connector_id=connector.id, name="notes.txt", provider_file_id="f-b")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    # The same name in two folders keeps both, each in its own place.
    _archive_step(db, plan_id=plan.id, file_id=a.id, path="Site/docs/notes.txt", order=0)
    _archive_step(db, plan_id=plan.id, file_id=b.id, path="Site/assets/notes.txt", order=1)
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)
    drive = _Drive(
        files={
            "f-a": _drive_file(file_id="f-a", name="notes.txt"),
            "f-b": _drive_file(file_id="f-b", name="notes.txt"),
        }
    )
    _engine(db, drive).run(job.id)
    db.expire_all()
    [upload_id] = list(drive.uploads)
    recorded = db.query(File).filter_by(provider_file_id=upload_id).one()
    return drive, user, recorded


@requires_infra
def test_a_drive_archive_keeps_folder_structure_and_shows_in_files(db: Session) -> None:
    drive, _user, recorded = _archive(db)

    with zipfile.ZipFile(io.BytesIO(drive.uploads[recorded.provider_file_id])) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
    assert {"Site/docs/notes.txt", "Site/assets/notes.txt", "manifest.json"} <= names
    assert recorded.path.startswith("/AI Vault Archive/")


@requires_infra
def test_extracting_a_drive_archive_recreates_its_folders(db: Session) -> None:
    drive, user, recorded = _archive(db)

    summary = _engine(db, drive).extract_archive(
        organization_id=user.organization_id, file_id=recorded.id
    )

    db.expire_all()
    paths = {
        file.path
        for file in FileRepository(db).list_under_folder(
            recorded.storage_source_id, summary.folder.path
        )
    }
    base = summary.folder.path
    assert {f"{base}/Site/docs/notes.txt", f"{base}/Site/assets/notes.txt"} <= paths
    assert (
        db.query(Folder)
        .filter_by(storage_source_id=recorded.storage_source_id, name="docs")
        .count()
    )


@requires_infra
def test_a_drive_extraction_skips_entries_that_would_escape_its_folder(db: Session) -> None:
    drive, user, recorded = _archive(db)
    hostile = io.BytesIO()
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr("../../outside.txt", "nope")
        archive.writestr("ok/inside.txt", "ok")
    drive.uploads[recorded.provider_file_id] = hostile.getvalue()

    summary = _engine(db, drive).extract_archive(
        organization_id=user.organization_id, file_id=recorded.id
    )

    assert summary.file_count == 1
    assert [item["name"] for item in summary.skipped] == ["../../outside.txt"]


def _archive_named(db: Session, *, name: str, taken: bool) -> tuple[File, Folder]:
    """Archives one file under the name the user chose, into a folder they
    chose — which already holds a file of that name when `taken`."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="notes.txt", provider_file_id="f-a")
    backups = FolderRepository(db).upsert(
        storage_source_id=file.storage_source_id,
        provider_file_id="d-backups",
        provider_parent_id=None,
        parent_folder_id=None,
        name="Backups",
        path="/Backups",
        owner_email=None,
        is_shared=False,
        provider_created_at=None,
        provider_modified_at=None,
        scanned_at=datetime.now(UTC),
    )
    if taken:
        existing = _provision_file(
            db, connector_id=connector.id, name=name, provider_file_id="f-existing"
        )
        existing.parent_folder_id = backups.id
    db.commit()
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    _archive_step(
        db,
        plan_id=plan.id,
        file_id=file.id,
        path="notes.txt",
        order=0,
        archive_name=name,
        archive_folder_id=str(backups.id),
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)
    drive = _Drive(files={"f-a": _drive_file(file_id="f-a", name="notes.txt")})
    _engine(db, drive).run(job.id)
    db.expire_all()
    [upload_id] = list(drive.uploads)
    return db.query(File).filter_by(provider_file_id=upload_id).one(), backups


@requires_infra
def test_an_archive_gets_the_name_and_folder_the_user_chose(db: Session) -> None:
    recorded, backups = _archive_named(db, name="Project_Docs_Archive.zip", taken=False)

    assert recorded.name == "Project_Docs_Archive.zip"
    assert recorded.parent_folder_id == backups.id


@requires_infra
def test_a_chosen_name_already_in_the_folder_is_numbered_not_overwritten(db: Session) -> None:
    recorded, _ = _archive_named(db, name="Project_Docs_Archive.zip", taken=True)

    assert recorded.name == "Project_Docs_Archive (2).zip"


@requires_infra
def test_a_drive_archive_can_be_listed_without_extracting_it(db: Session) -> None:
    drive, user, recorded = _archive(db)
    uploads_before = len(drive.uploads)

    contents = _engine(db, drive).inspect_archive(
        organization_id=user.organization_id, file_id=recorded.id
    )

    names = {entry["name"] for entry in contents["entries"]}
    assert {"Site/docs/notes.txt", "Site/assets/notes.txt"} <= names
    assert contents["unsafe_count"] == 0
    assert len(drive.uploads) == uploads_before
