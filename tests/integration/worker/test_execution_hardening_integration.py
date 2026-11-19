"""Phase 0 hardening of the Execution Engine's mutation point.

* Every mutation is scoped to the plan's own organization, inside the engine
  — plan creation already checks ownership, but the worker must not trust it.
* A step redelivered after a worker crash (`acks_late`) reuses the rollback
  record it captured the first time and does not repeat a Drive write that
  already landed.
* That tolerance is state-based, not error-swallowing: a file that matches
  neither its captured pre-state nor the state the step produces was changed
  by someone else, and is reported as a conflict instead of overwritten.
* Only one worker executes a plan at a time; finished jobs are never re-run.

A simulated crash is a `BaseException` raised by the fake Drive *after* its
write lands, so it escapes the engine's `except Exception` boundaries the way
a killed process would.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_execution_service_integration import (
    _drive_file,
    _FakeGoogleDriveClient,
    _FakeGoogleWorkspaceOAuthClient,
    _FakeObjectStorageClient,
    _provision_ad_hoc_plan,
    _provision_connector,
    _provision_file,
    _provision_job,
    _provision_user,
    requires_infra,
)

from vault_shared.db.models import (
    ExecutionActionType,
    ExecutionJobStatus,
    ExecutionResultStatus,
    ExecutionStepStatus,
    RollbackRecord,
)
from vault_shared.db.repositories import (
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
    RollbackRecordRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
from vault_shared.storage.default_registry import build_storage_registry
from worker.execution.execution_service import ExecutionService, plan_lock_key

_MUTATING_CALLS = {
    "set_trashed",
    "rename_file",
    "move_file",
    "delete_file",
    "update_app_properties",
}
_CROSS_TENANT_ERROR = "Target file no longer exists in this platform's inventory."
_COMPLETED = ExecutionStepStatus.COMPLETED


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _WorkerCrash(BaseException):
    """Not an `Exception` — nothing in the engine may catch it."""


class _CrashingDrive(_FakeGoogleDriveClient):
    """Performs the named write, then dies before the engine can record it."""

    def __init__(self, *, crash_after: str, files: dict) -> None:
        super().__init__(files=files)
        self._crash_after = crash_after
        self.armed = True

    def _crash_if_armed(self, method: str) -> None:
        if self.armed and method == self._crash_after:
            self.armed = False
            raise _WorkerCrash

    def set_trashed(self, **kwargs):
        result = super().set_trashed(**kwargs)
        self._crash_if_armed("set_trashed")
        return result

    def rename_file(self, **kwargs):
        result = super().rename_file(**kwargs)
        self._crash_if_armed("rename_file")
        return result

    def move_file(self, **kwargs):
        result = super().move_file(**kwargs)
        self._crash_if_armed("move_file")
        return result

    def delete_file(self, **kwargs):
        super().delete_file(**kwargs)
        self._crash_if_armed("delete_file")


def _service(db: Session, drive: _FakeGoogleDriveClient) -> ExecutionService:
    return ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )


def _mutations(drive: _FakeGoogleDriveClient) -> list[tuple[str, str]]:
    return [call for call in drive.calls if call[0] in _MUTATING_CALLS]


def _step(
    db: Session, *, plan_id: uuid.UUID, file_id: uuid.UUID, action_type: str, planned_change: dict
):
    step = ExecutionStepRepository(db).create(
        execution_plan_id=plan_id,
        step_order=0,
        action_type=action_type,
        target_file_id=file_id,
        pre_state={},
        planned_change=planned_change,
    )
    db.commit()
    return step


def _org_with_file(db: Session, *, provider_file_id: str, name: str = "Doc.txt"):
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(
        db, connector_id=connector.id, name=name, provider_file_id=provider_file_id
    )
    plan = _provision_ad_hoc_plan(db, organization_id=user.organization_id, user_id=user.id)
    return user, file, plan


def _new_job(db: Session, *, plan, user, is_rollback: bool = False):
    return _provision_job(
        db,
        plan_id=plan.id,
        organization_id=user.organization_id,
        user_id=user.id,
        is_rollback=is_rollback,
    )


def _run(db: Session, drive: _FakeGoogleDriveClient, *, plan, user, is_rollback: bool = False):
    job = _new_job(db, plan=plan, user=user, is_rollback=is_rollback)
    _service(db, drive).run(job.id)
    db.expire_all()
    return ExecutionJobRepository(db).get_by_id(job.id)


def _crash_then_redeliver(db: Session, drive: _CrashingDrive, *, plan, user):
    """First delivery dies right after the Drive write; the same job is then
    redelivered to a fresh worker session."""
    job = _new_job(db, plan=plan, user=user)
    with pytest.raises(_WorkerCrash):
        _service(db, drive).run(job.id)
    db.rollback()

    retry_session = get_session_factory()()
    try:
        _service(retry_session, drive).run(job.id)
    finally:
        retry_session.close()
    db.expire_all()
    return ExecutionJobRepository(db).get_by_id(job.id)


def _step_status(db: Session, step) -> str:
    db.expire_all()
    return ExecutionStepRepository(db).get_by_id(step.id).status


def _audit_types(db: Session, plan) -> list[str]:
    return [entry.event_type for entry in ExecutionAuditRepository(db).list_for_plan(plan.id)]


def _record_count(db: Session, step) -> int:
    return db.query(RollbackRecord).filter_by(execution_step_id=step.id).count()


class TestTenantIsolationAtTheMutationPoint:
    @requires_infra
    def test_a_step_targeting_another_organizations_file_is_refused_and_nothing_is_touched(
        self, db: Session
    ) -> None:
        attacker, _own_file, plan = _org_with_file(db, provider_file_id="f-own")
        _victim, victim_file, _victim_plan = _org_with_file(db, provider_file_id="f-victim")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=victim_file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(
            files={
                "f-own": _drive_file(file_id="f-own"),
                "f-victim": _drive_file(file_id="f-victim"),
            }
        )

        job = _run(db, drive, plan=plan, user=attacker)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert job.status == ExecutionJobStatus.FAILED
        assert _mutations(drive) == []
        assert ("get_file", "f-victim") not in drive.calls
        assert drive._files["f-victim"].trashed is False
        assert FileRepository(db).get_by_id(victim_file.id).trashed is False
        assert RollbackRecordRepository(db).get_by_step(step.id) is None

    @requires_infra
    def test_the_refusal_is_recorded_without_revealing_anything_about_the_other_tenant(
        self, db: Session
    ) -> None:
        attacker, _own_file, plan = _org_with_file(db, provider_file_id="f-own")
        _victim, victim_file, _victim_plan = _org_with_file(
            db, provider_file_id="f-victim", name="Victim-Secret-Contract.pdf"
        )
        step = _step(
            db,
            plan_id=plan.id,
            file_id=victim_file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-victim": _drive_file(file_id="f-victim")})

        job = _run(db, drive, plan=plan, user=attacker)

        result = ExecutionResultRepository(db).get_by_job_and_step(
            execution_job_id=job.id, execution_step_id=step.id
        )
        assert result.status == ExecutionResultStatus.FAILED
        assert result.error == _CROSS_TENANT_ERROR
        audit = [
            entry
            for entry in ExecutionAuditRepository(db).list_for_job(job.id)
            if entry.event_type == "step_failed"
        ]
        assert [entry.message for entry in audit] == [_CROSS_TENANT_ERROR]
        rendered = repr([(entry.message, entry.metadata_) for entry in audit]) + str(result.error)
        assert "Victim-Secret" not in rendered
        assert "f-victim" not in rendered

    @requires_infra
    def test_a_missing_file_and_another_organizations_file_produce_the_same_error(
        self, db: Session
    ) -> None:
        attacker, _own_file, plan = _org_with_file(db, provider_file_id="f-own")
        _victim, victim_file, _victim_plan = _org_with_file(db, provider_file_id="f-victim")
        foreign_step = _step(
            db,
            plan_id=plan.id,
            file_id=victim_file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={})

        job = _run(db, drive, plan=plan, user=attacker)

        result = ExecutionResultRepository(db).get_by_job_and_step(
            execution_job_id=job.id, execution_step_id=foreign_step.id
        )
        assert result.error == _CROSS_TENANT_ERROR

    @requires_infra
    def test_a_rollback_of_another_organizations_file_is_refused(self, db: Session) -> None:
        attacker, _own_file, plan = _org_with_file(db, provider_file_id="f-own")
        _victim, victim_file, _victim_plan = _org_with_file(db, provider_file_id="f-victim")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=victim_file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        RollbackRecordRepository(db).create(execution_step_id=step.id, pre_state={"trashed": False})
        db.commit()
        drive = _FakeGoogleDriveClient(
            files={"f-victim": _drive_file(file_id="f-victim", trashed=True)}
        )

        _run(db, drive, plan=plan, user=attacker, is_rollback=True)

        assert _mutations(drive) == []
        assert drive._files["f-victim"].trashed is True
        assert RollbackRecordRepository(db).get_by_step(step.id).rolled_back is False

    @requires_infra
    def test_an_archive_batch_never_downloads_another_organizations_file(self, db: Session) -> None:
        attacker, _own_file, plan = _org_with_file(db, provider_file_id="f-own")
        _victim, victim_file, _victim_plan = _org_with_file(db, provider_file_id="f-victim")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=victim_file.id,
            action_type=ExecutionActionType.CREATE_ARCHIVE,
            planned_change={"action": "create_archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-victim": _drive_file(file_id="f-victim")})

        _run(db, drive, plan=plan, user=attacker)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert ("download_file", "f-victim") not in drive.calls
        assert ("export_file", "f-victim") not in drive.calls

    @requires_infra
    def test_a_step_on_the_plans_own_file_still_executes(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-own")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-own": _drive_file(file_id="f-own")})

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert _mutations(drive) == [("set_trashed", "f-own")]


class TestRedeliveryAfterACrash:
    @requires_infra
    def test_a_trash_that_landed_before_the_crash_is_not_repeated_or_reported_as_a_conflict(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.REMOVE_DUPLICATE,
            planned_change={"action": "remove_duplicate"},
        )
        drive = _CrashingDrive(crash_after="set_trashed", files={"f-1": _drive_file(file_id="f-1")})

        job = _crash_then_redeliver(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert job.status == ExecutionJobStatus.COMPLETED
        assert [call for call in drive.calls if call[0] == "set_trashed"] == [
            ("set_trashed", "f-1")
        ]
        assert _record_count(db, step) == 1
        assert RollbackRecordRepository(db).get_by_step(step.id).pre_state == {"trashed": False}
        assert FileRepository(db).get_by_id(file.id).trashed is True
        audit = _audit_types(db, plan)
        assert "step_already_applied" in audit
        assert "step_failed" not in audit

    @requires_infra
    def test_a_rename_that_landed_before_the_crash_keeps_its_original_pre_state(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Old.txt")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.RENAME,
            planned_change={"action": "rename", "new_name": "New.txt"},
        )
        drive = _CrashingDrive(
            crash_after="rename_file", files={"f-1": _drive_file(file_id="f-1", name="Old.txt")}
        )

        job = _crash_then_redeliver(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert job.status == ExecutionJobStatus.COMPLETED
        assert [call for call in drive.calls if call[0] == "rename_file"] == [
            ("rename_file", "f-1")
        ]
        assert _record_count(db, step) == 1
        assert RollbackRecordRepository(db).get_by_step(step.id).pre_state == {"name": "Old.txt"}
        assert "step_already_applied" in _audit_types(db, plan)

    @requires_infra
    def test_a_move_that_landed_before_the_crash_is_not_repeated(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.MOVE_FILE,
            planned_change={"action": "move_file", "new_parent_id": "folder-b"},
        )
        drive = _CrashingDrive(
            crash_after="move_file",
            files={"f-1": replace(_drive_file(file_id="f-1"), parents=["folder-a"])},
        )

        job = _crash_then_redeliver(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert job.status == ExecutionJobStatus.COMPLETED
        assert [call for call in drive.calls if call[0] == "move_file"] == [("move_file", "f-1")]
        assert drive._files["f-1"].parents == ["folder-b"]
        assert RollbackRecordRepository(db).get_by_step(step.id).pre_state == {
            "parent_folder_id": "folder-a"
        }

    @requires_infra
    def test_a_permanent_delete_that_landed_before_the_crash_is_completed_and_mirrored(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        FileRepository(db).mark_trashed(file, trashed=True)
        db.commit()
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.PERMANENT_DELETE,
            planned_change={"action": "permanent_delete"},
        )
        drive = _CrashingDrive(
            crash_after="delete_file",
            files={"f-1": _drive_file(file_id="f-1", trashed=True)},
        )

        job = _crash_then_redeliver(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert job.status == ExecutionJobStatus.COMPLETED
        assert [call for call in drive.calls if call[0] == "delete_file"] == [
            ("delete_file", "f-1")
        ]
        assert FileRepository(db).get_by_id(file.id).permanently_deleted_at is not None
        assert _record_count(db, step) == 1

    @requires_infra
    def test_a_write_that_never_landed_is_applied_exactly_once_on_retry(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Old.txt")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.RENAME,
            planned_change={"action": "rename", "new_name": "New.txt"},
        )
        RollbackRecordRepository(db).create(
            execution_step_id=step.id, pre_state={"name": "Old.txt"}
        )
        db.commit()
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Old.txt")})

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == _COMPLETED
        assert _mutations(drive) == [("rename_file", "f-1")]
        assert drive._files["f-1"].name == "New.txt"
        assert _record_count(db, step) == 1
        assert "step_already_applied" not in _audit_types(db, plan)


class TestRedeliveryOfAFinishedOrRunningJob:
    @requires_infra
    @pytest.mark.parametrize("finish", ["completed", "failed", "cancelled", "paused"])
    def test_a_job_that_is_not_pending_or_running_is_never_re_run(
        self, db: Session, finish: str
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        job = _new_job(db, plan=plan, user=user)
        jobs = ExecutionJobRepository(db)
        {
            "completed": lambda: jobs.mark_completed(job),
            "failed": lambda: jobs.mark_failed(job, error="earlier failure"),
            "cancelled": lambda: jobs.mark_cancelled(job),
            "paused": lambda: jobs.mark_paused(job),
        }[finish]()
        db.commit()
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1")})

        _service(db, drive).run(job.id)

        assert drive.calls == []
        assert _step_status(db, step) == ExecutionStepStatus.PENDING
        assert _audit_types(db, plan) == []

    @requires_infra
    def test_redelivery_of_a_job_that_already_completed_changes_nothing(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1")})
        job = _run(db, drive, plan=plan, user=user)
        calls_after_first_delivery = list(drive.calls)
        audit_after_first_delivery = _audit_types(db, plan)

        _service(db, drive).run(job.id)
        db.expire_all()

        assert drive.calls == calls_after_first_delivery
        assert _audit_types(db, plan) == audit_after_first_delivery
        assert ExecutionJobRepository(db).get_by_id(job.id).status == ExecutionJobStatus.COMPLETED
        assert _step_status(db, step) == _COMPLETED
        assert _record_count(db, step) == 1

    @requires_infra
    def test_a_duplicate_delivery_while_another_worker_holds_the_plan_does_nothing(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        job = _new_job(db, plan=plan, user=user)
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1")})
        key = {"key": plan_lock_key(plan.id)}

        with db.get_bind().connect() as other_worker:
            other_worker.execute(text("SELECT pg_advisory_lock(:key)"), key)
            try:
                _service(db, drive).run(job.id)
                db.expire_all()

                assert drive.calls == []
                assert ExecutionJobRepository(db).get_by_id(job.id).status == (
                    ExecutionJobStatus.PENDING
                )
                assert _step_status(db, step) == ExecutionStepStatus.PENDING
            finally:
                other_worker.execute(text("SELECT pg_advisory_unlock(:key)"), key)

        _service(db, drive).run(job.id)
        assert _step_status(db, step) == _COMPLETED
        assert _mutations(drive) == [("set_trashed", "f-1")]

    @requires_infra
    def test_the_plan_lock_is_released_after_a_crash(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        job = _new_job(db, plan=plan, user=user)
        drive = _CrashingDrive(crash_after="set_trashed", files={"f-1": _drive_file(file_id="f-1")})
        with pytest.raises(_WorkerCrash):
            _service(db, drive).run(job.id)
        db.rollback()

        with db.get_bind().connect() as probe:
            acquired = probe.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": plan_lock_key(plan.id)}
            ).scalar()
            if acquired:
                probe.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": plan_lock_key(plan.id)}
                )

        assert acquired is True


class TestGenuineConflictsAreNotHidden:
    @requires_infra
    def test_a_rename_retry_finding_a_third_name_is_a_conflict_and_is_not_overwritten(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Old.txt")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.RENAME,
            planned_change={"action": "rename", "new_name": "New.txt"},
        )
        RollbackRecordRepository(db).create(
            execution_step_id=step.id, pre_state={"name": "Old.txt"}
        )
        db.commit()
        drive = _FakeGoogleDriveClient(
            files={"f-1": _drive_file(file_id="f-1", name="Renamed-By-Someone-Else.txt")}
        )

        job = _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert job.status == ExecutionJobStatus.FAILED
        assert _mutations(drive) == []
        assert drive._files["f-1"].name == "Renamed-By-Someone-Else.txt"
        assert RollbackRecordRepository(db).get_by_step(step.id).pre_state == {"name": "Old.txt"}
        failures = [
            entry
            for entry in ExecutionAuditRepository(db).list_for_job(job.id)
            if entry.event_type == "step_failed"
        ]
        assert len(failures) == 1
        assert failures[0].metadata_["error_type"] == "ConflictError"
        assert "someone else" in failures[0].message
        assert "step_already_applied" not in _audit_types(db, plan)

    @requires_infra
    def test_a_move_retry_finding_a_third_parent_is_a_conflict(self, db: Session) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.MOVE_FILE,
            planned_change={"action": "move_file", "new_parent_id": "folder-b"},
        )
        RollbackRecordRepository(db).create(
            execution_step_id=step.id, pre_state={"parent_folder_id": "folder-a"}
        )
        db.commit()
        drive = _FakeGoogleDriveClient(
            files={"f-1": replace(_drive_file(file_id="f-1"), parents=["folder-c"])}
        )

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert _mutations(drive) == []
        assert drive._files["f-1"].parents == ["folder-c"]

    @requires_infra
    def test_a_permanent_delete_retry_finding_the_file_restored_is_a_conflict(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        FileRepository(db).mark_trashed(file, trashed=True)
        db.commit()
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.PERMANENT_DELETE,
            planned_change={"action": "permanent_delete"},
        )
        RollbackRecordRepository(db).create(execution_step_id=step.id, pre_state={})
        db.commit()
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", trashed=False)})

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert _mutations(drive) == []
        assert "f-1" in drive._files

    @requires_infra
    def test_a_first_attempt_permanent_delete_of_a_file_no_longer_in_trash_is_refused(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        FileRepository(db).mark_trashed(file, trashed=True)
        db.commit()
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.PERMANENT_DELETE,
            planned_change={"action": "permanent_delete"},
        )
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", trashed=False)})

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert _mutations(drive) == []
        assert FileRepository(db).get_by_id(file.id).permanently_deleted_at is None
        assert RollbackRecordRepository(db).get_by_step(step.id) is None

    @requires_infra
    def test_a_permanent_delete_of_a_missing_file_with_no_prior_attempt_fails(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        FileRepository(db).mark_trashed(file, trashed=True)
        db.commit()
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.PERMANENT_DELETE,
            planned_change={"action": "permanent_delete"},
        )

        _run(db, _FakeGoogleDriveClient(files={}), plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert FileRepository(db).get_by_id(file.id).permanently_deleted_at is None

    @requires_infra
    def test_a_file_trashed_outside_the_platform_is_still_a_conflict_on_the_first_attempt(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", trashed=True)})

        _run(db, drive, plan=plan, user=user)

        assert _step_status(db, step) == ExecutionStepStatus.FAILED
        assert RollbackRecordRepository(db).get_by_step(step.id) is None
        assert _mutations(drive) == []


class TestRollbackIsRetryableAndDoesNotClobber:
    def _completed_rename(self, db: Session):
        user, file, plan = _org_with_file(db, provider_file_id="f-1", name="Old.txt")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.RENAME,
            planned_change={"action": "rename", "new_name": "New.txt"},
        )
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Old.txt")})
        _run(db, drive, plan=plan, user=user)
        assert drive._files["f-1"].name == "New.txt"
        return user, plan, step, drive

    @requires_infra
    def test_a_rollback_that_already_landed_is_not_repeated(self, db: Session) -> None:
        user, plan, step, drive = self._completed_rename(db)
        drive._files["f-1"] = replace(drive._files["f-1"], name="Old.txt")
        writes_before = len(_mutations(drive))

        job = _run(db, drive, plan=plan, user=user, is_rollback=True)

        assert len(_mutations(drive)) == writes_before
        assert job.status == ExecutionJobStatus.COMPLETED
        assert RollbackRecordRepository(db).get_by_step(step.id).rolled_back is True

    @requires_infra
    def test_a_rollback_of_a_file_renamed_by_someone_else_is_refused(self, db: Session) -> None:
        user, plan, step, drive = self._completed_rename(db)
        drive._files["f-1"] = replace(drive._files["f-1"], name="Someone-Elses-Name.txt")
        writes_before = len(_mutations(drive))

        job = _run(db, drive, plan=plan, user=user, is_rollback=True)

        assert len(_mutations(drive)) == writes_before
        assert drive._files["f-1"].name == "Someone-Elses-Name.txt"
        assert job.status == ExecutionJobStatus.FAILED
        assert RollbackRecordRepository(db).get_by_step(step.id).rolled_back is False

    @requires_infra
    def test_a_normal_rollback_still_restores_the_original_name(self, db: Session) -> None:
        user, plan, step, drive = self._completed_rename(db)

        job = _run(db, drive, plan=plan, user=user, is_rollback=True)

        assert drive._files["f-1"].name == "Old.txt"
        assert job.status == ExecutionJobStatus.COMPLETED


class TestKnownIssues:
    """Documented, unfixed behavior — kept as strict expected-failures so the
    day it is fixed the suite says so and this marker gets removed."""

    @requires_infra
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN ISSUE (Phase 0 report): execution_steps.target_file_id and "
            "rollback_records/execution_results reference files with ON DELETE CASCADE, and the "
            "incremental sync hard-deletes a File row when Drive reports the item trashed "
            "(ScannerService._remove_item). A completed step, its result and its rollback record "
            "therefore vanish at the next incremental sync after the engine trashes a file."
        ),
    )
    def test_a_completed_steps_rollback_record_survives_the_file_row_being_removed(
        self, db: Session
    ) -> None:
        user, file, plan = _org_with_file(db, provider_file_id="f-1")
        step = _step(
            db,
            plan_id=plan.id,
            file_id=file.id,
            action_type=ExecutionActionType.ARCHIVE,
            planned_change={"action": "archive"},
        )
        drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1")})
        _run(db, drive, plan=plan, user=user)
        assert RollbackRecordRepository(db).get_by_step(step.id) is not None

        # What incremental sync does when Drive's change feed reports the
        # (now trashed) file.
        FileRepository(db).delete_by_source_and_provider_id(
            storage_source_id=file.storage_source_id, provider_file_id="f-1"
        )
        db.commit()
        db.expire_all()

        assert ExecutionStepRepository(db).get_by_id(step.id) is not None
        assert RollbackRecordRepository(db).get_by_step(step.id) is not None
