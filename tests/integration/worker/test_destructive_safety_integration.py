"""Gate 1 at the mutation point: whatever built a plan, the Execution Engine
itself refuses to remove or relocate protected files (source code, project
files, program files) unless the user who picked them acknowledged it, and
refuses to act on a file that changed since the user reviewed it.

Everything runs against a fake Drive — no real storage is touched.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_execution_hardening_integration import (
    _mutations,
    _org_with_file,
    _run,
    _step_status,
)
from test_execution_service_integration import (
    _drive_file,
    _FakeGoogleDriveClient,
    requires_infra,
)

from vault_shared.db.models import (
    ConnectorStatus,
    ExecutionActionType,
    ExecutionJobStatus,
    ExecutionResult,
    ExecutionStepStatus,
)
from vault_shared.db.repositories import (
    ExecutionStepRepository,
    FileRepository,
    StorageConnectorRepository,
)
from vault_shared.db.session import get_session_factory

_TRASH = ExecutionActionType.ARCHIVE


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _file_at(db: Session, *, like, path: str, provider_file_id: str):  # noqa: ANN001, ANN202
    """A file in the same storage as `like`, at `path`."""
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=like.storage_source_id,
        provider_file_id=provider_file_id,
        provider_parent_id=None,
        parent_folder_id=None,
        name=path.rsplit("/", 1)[-1],
        path=path,
        mime_type="text/plain",
        size_bytes=100,
        owner_email="founder@acme.com",
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    db.commit()
    return file


def _step(db: Session, *, plan, file, action_type=_TRASH, planned_change=None, pre_state=None):  # noqa: ANN001, ANN202
    step = ExecutionStepRepository(db).create(
        execution_plan_id=plan.id,
        step_order=0,
        action_type=action_type,
        target_file_id=file.id,
        pre_state=pre_state or {},
        planned_change=planned_change or {"action": action_type},
    )
    db.commit()
    return step


def _error(db: Session, step) -> str:  # noqa: ANN001
    results = db.query(ExecutionResult).filter_by(execution_step_id=step.id).all()
    return " ".join(result.error or "" for result in results)


@requires_infra
def test_a_python_file_is_not_trashed_even_when_a_plan_says_to(db: Session) -> None:
    user, file, plan = _org_with_file(db, provider_file_id="f-py", name="model_utils.py")
    step = _step(db, plan=plan, file=file)
    drive = _FakeGoogleDriveClient(
        files={"f-py": _drive_file(file_id="f-py", name="model_utils.py")}
    )

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.FAILED
    assert _mutations(drive) == []
    assert "protected" in _error(db, step)


@requires_infra
def test_a_picture_inside_a_software_project_is_not_trashed(db: Session) -> None:
    user, marker, plan = _org_with_file(db, provider_file_id="f-pkg", name="package.json")
    FileRepository(db).mirror_location(
        marker, name="package.json", provider_parent_id=None, parent_folder_id=None,
        path="/shop-site/package.json",
    )  # fmt: skip
    logo = _file_at(db, like=marker, path="/shop-site/public/logo.png", provider_file_id="f-logo")
    step = _step(db, plan=plan, file=logo)
    drive = _FakeGoogleDriveClient(files={"f-logo": _drive_file(file_id="f-logo", name="logo.png")})

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.FAILED
    assert _mutations(drive) == []
    assert "software project in /shop-site" in _error(db, step)


@requires_infra
def test_a_file_outside_any_project_is_trashed_as_planned(db: Session) -> None:
    user, marker, plan = _org_with_file(db, provider_file_id="f-pkg", name="package.json")
    FileRepository(db).mirror_location(
        marker, name="package.json", provider_parent_id=None, parent_folder_id=None,
        path="/shop-site/package.json",
    )  # fmt: skip
    photo = _file_at(db, like=marker, path="/Photos/beach.jpg", provider_file_id="f-beach")
    step = _step(db, plan=plan, file=photo)
    drive = _FakeGoogleDriveClient(
        files={"f-beach": _drive_file(file_id="f-beach", name="beach.jpg")}
    )

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.COMPLETED
    assert _mutations(drive) == [("set_trashed", "f-beach")]


@requires_infra
def test_a_protected_file_the_user_picked_and_acknowledged_is_trashed(db: Session) -> None:
    user, file, plan = _org_with_file(db, provider_file_id="f-py", name="old_script.py")
    step = _step(
        db,
        plan=plan,
        file=file,
        planned_change={
            "action": _TRASH,
            "protection_override": {"confirmed_by_user_id": str(user.id), "reasons": ["x"]},
        },
    )
    drive = _FakeGoogleDriveClient(
        files={"f-py": _drive_file(file_id="f-py", name="old_script.py")}
    )

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.COMPLETED
    assert _mutations(drive) == [("set_trashed", "f-py")]


@requires_infra
def test_a_file_renamed_since_it_was_reviewed_is_not_trashed(db: Session) -> None:
    user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Report.pdf")
    step = _step(db, plan=plan, file=file, pre_state={"name": "Report.pdf"})
    drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Contract.pdf")})

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.FAILED
    assert _mutations(drive) == []
    assert "now called" in _error(db, step)


@requires_infra
def test_a_file_moved_since_it_was_reviewed_is_not_trashed(db: Session) -> None:
    user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Report.pdf")
    step = _step(
        db, plan=plan, file=file, pre_state={"name": "Report.pdf", "provider_parent_id": "folder-a"}
    )
    drive = _FakeGoogleDriveClient(
        files={"f-1": _drive_file(file_id="f-1", name="Report.pdf", parents=["folder-b"])}
    )

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.FAILED
    assert _mutations(drive) == []


@requires_infra
def test_a_disconnected_storage_fails_the_job_and_touches_nothing(db: Session) -> None:
    user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Report.pdf")
    step = _step(db, plan=plan, file=file)
    connector = StorageConnectorRepository(db).get_by_organization_and_provider(
        organization_id=user.organization_id, provider="google_workspace"
    )
    connector.status = ConnectorStatus.REAUTH_REQUIRED
    db.commit()
    drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Report.pdf")})

    job = _run(db, drive, plan=plan, user=user)

    assert job.status == ExecutionJobStatus.FAILED
    assert _step_status(db, step) == ExecutionStepStatus.PENDING
    assert _mutations(drive) == []
