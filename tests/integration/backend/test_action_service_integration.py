import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session
from test_execution_service_integration import (
    _provision_connector,
    _provision_file,
    _provision_user,
    requires_infra,
)

from app.application.action_service import ActionService
from vault_shared import NotFoundError
from vault_shared.db.models import ExecutionActionType, ExecutionPlanStatus
from vault_shared.db.repositories import (
    ArchiveJobRepository,
    ExecutionPlanRepository,
    ExecutionStepRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
from vault_shared.execution.plan_service import ExecutionPlanService


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _trash_plan(db: Session, *, file_names: tuple[str, ...] = ("a.pdf", "b.pdf")):
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    files = [
        _provision_file(
            db, connector_id=connector.id, name=name, provider_file_id=f"drv-{uuid.uuid4().hex}"
        )
        for name in file_names
    ]
    plan = ExecutionPlanService(db).create_ad_hoc_plan(
        [file.id for file in files],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=user.organization_id,
        user_id=user.id,
    )
    db.commit()
    return user, files, plan


def _service(db: Session) -> ActionService:
    return ActionService(db, job_service=MagicMock())


def _finish(db: Session, plan, *, failed_file_ids: set[uuid.UUID] = frozenset()) -> None:
    steps = ExecutionStepRepository(db)
    for step in steps.list_for_plan(plan.id):
        if step.target_file_id in failed_file_ids:
            steps.mark_failed(step)
        else:
            steps.mark_completed(step)
    status = (
        ExecutionPlanStatus.PARTIALLY_COMPLETED
        if failed_file_ids
        else ExecutionPlanStatus.COMPLETED
    )
    ExecutionPlanRepository(db).update_status(plan, status=status)
    db.commit()


@requires_infra
def test_an_unfinished_action_reads_as_in_progress_in_plain_language(db: Session) -> None:
    user, _, plan = _trash_plan(db)

    action = _service(db).get(plan.id, organization_id=user.organization_id)

    assert action.status == "in_progress"
    assert action.message == "Moving 2 files to Trash in Google Drive…"


@requires_infra
def test_a_finished_action_reports_what_the_provider_did(db: Session) -> None:
    user, _, plan = _trash_plan(db)
    _finish(db, plan)

    action = _service(db).get(plan.id, organization_id=user.organization_id)

    assert action.status == "done"
    assert action.message.startswith("2 of 2 files moved to Trash in Google Drive")


@requires_infra
def test_a_partly_failed_action_names_the_file_that_failed(db: Session) -> None:
    user, files, plan = _trash_plan(db)
    _finish(db, plan, failed_file_ids={files[1].id})

    action = _service(db).get(plan.id, organization_id=user.organization_id)

    assert action.status == "partial"
    assert [problem.file_name for problem in action.problems] == ["b.pdf"]


@requires_infra
def test_another_organizations_action_is_not_found(db: Session) -> None:
    _, _, plan = _trash_plan(db)
    stranger = _provision_user(db)

    with pytest.raises(NotFoundError):
        _service(db).get(plan.id, organization_id=stranger.organization_id)


@requires_infra
def test_activity_skips_an_action_whose_files_no_longer_exist(db: Session) -> None:
    user, files, plan = _trash_plan(db)
    _finish(db, plan)
    for file in files:
        db.delete(file)
    db.commit()

    activity = _service(db).recent_activity(user.organization_id)

    assert str(plan.id) not in {item.id for item in activity}


@requires_infra
def test_an_archive_that_kept_originals_says_where_the_zip_went(db: Session) -> None:
    user, files, _ = _trash_plan(db, file_names=("report.pdf",))
    plan = ExecutionPlanService(db).create_ad_hoc_plan(
        [files[0].id],
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        organization_id=user.organization_id,
        user_id=user.id,
    )
    db.commit()
    archive_job = ArchiveJobRepository(db).create(
        organization_id=user.organization_id,
        execution_plan_id=plan.id,
        name="Archive",
        created_by_user_id=user.id,
    )
    ArchiveJobRepository(db).mark_completed(
        archive_job,
        object_storage_key=None,
        original_size_bytes=10,
        compressed_size_bytes=10,
        file_count=1,
        manifest=[],
        destination_path="/AI Vault Archive/2026/a.zip",
    )
    _finish(db, plan)

    action = _service(db).get(plan.id, organization_id=user.organization_id)

    assert action.message == (
        "1 file zipped into 'AI Vault Archive/2026' in Google Drive — originals kept where they were"
    )
