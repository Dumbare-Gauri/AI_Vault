import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from vault_shared import get_settings
from vault_shared.db.models import RoleName, WorkflowNodeType, WorkflowTriggerType
from vault_shared.db.repositories import (
    OrganizationRepository,
    RoleRepository,
    SchedulerJobRepository,
    UserRepository,
    WorkflowExecutionRepository,
    WorkflowNodeRepository,
    WorkflowRepository,
    WorkflowTriggerRepository,
    WorkflowVersionRepository,
)
from vault_shared.db.session import get_session_factory
from worker.workflow.scheduler_service import SchedulerService


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except OSError:
        return False


def _infra_available() -> bool:
    settings = get_settings()
    return _reachable(settings.database_url) and _reachable(settings.redis_url)


requires_infra = pytest.mark.skipif(
    not _infra_available(),
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI service "
    "containers.",
)


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _provision_user(db: Session):
    unique = uuid.uuid4().hex[:12]
    organization = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
    role = RoleRepository(db).get_by_name(RoleName.OWNER)
    user = UserRepository(db).create(
        organization_id=organization.id,
        role_id=role.id,
        google_sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        name="Ada Founder",
        avatar_url=None,
    )
    db.commit()
    return user


def _provision_published_workflow(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    workflow = WorkflowRepository(db).create(
        organization_id=organization_id, created_by_user_id=user_id, name="Test", description=None
    )
    version = WorkflowVersionRepository(db).create(
        workflow_id=workflow.id, version_number=1, created_by_user_id=user_id
    )
    trigger_node = WorkflowNodeRepository(db).create(
        workflow_version_id=version.id,
        node_type=WorkflowNodeType.TRIGGER,
        name="Start",
        config={},
        next_nodes={"default": "end"},
    )
    end_node = WorkflowNodeRepository(db).create(
        workflow_version_id=version.id,
        node_type=WorkflowNodeType.END,
        name="Done",
        config={},
        next_nodes={},
    )
    WorkflowNodeRepository(db).update_next_nodes(
        trigger_node, next_nodes={"default": str(end_node.id)}
    )
    WorkflowVersionRepository(db).publish(version)
    db.commit()
    return workflow, version


@requires_infra
def test_run_due_fires_a_due_scheduled_trigger_and_reschedules_it(db: Session) -> None:
    user = _provision_user(db)
    workflow, _version = _provision_published_workflow(
        db, organization_id=user.organization_id, user_id=user.id
    )
    trigger = WorkflowTriggerRepository(db).create(
        workflow_id=workflow.id,
        trigger_type=WorkflowTriggerType.SCHEDULED,
        config={"cron": "* * * * *"},
    )
    scheduler_jobs = SchedulerJobRepository(db)
    scheduler_jobs.upsert_for_trigger(
        trigger.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db.commit()

    # Other due triggers left in a shared database may fire too; only this
    # workflow's outcome is asserted.
    fired = SchedulerService(db).run_due()

    assert fired >= 1
    executions = WorkflowExecutionRepository(db).list_for_workflow(workflow.id)
    assert len(executions) == 1
    assert executions[0].trigger_type == WorkflowTriggerType.SCHEDULED

    scheduler_job = scheduler_jobs.get_by_trigger(trigger.id)
    assert scheduler_job.last_workflow_execution_id == executions[0].id
    assert scheduler_job.next_run_at is not None
    assert scheduler_job.next_run_at > datetime.now(UTC)


@requires_infra
def test_run_due_skips_a_paused_workflow_but_still_reschedules(db: Session) -> None:
    user = _provision_user(db)
    workflow, _version = _provision_published_workflow(
        db, organization_id=user.organization_id, user_id=user.id
    )
    WorkflowRepository(db).update_status(workflow, status="paused")
    trigger = WorkflowTriggerRepository(db).create(
        workflow_id=workflow.id,
        trigger_type=WorkflowTriggerType.SCHEDULED,
        config={"cron": "* * * * *"},
    )
    scheduler_jobs = SchedulerJobRepository(db)
    scheduler_jobs.upsert_for_trigger(
        trigger.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db.commit()

    fired = SchedulerService(db).run_due()

    assert fired == 0
    assert WorkflowExecutionRepository(db).list_for_workflow(workflow.id) == []

    scheduler_job = scheduler_jobs.get_by_trigger(trigger.id)
    assert scheduler_job.next_run_at is not None


@requires_infra
def test_run_due_does_not_double_fire_a_trigger_already_claimed(db: Session) -> None:
    user = _provision_user(db)
    workflow, _version = _provision_published_workflow(
        db, organization_id=user.organization_id, user_id=user.id
    )
    trigger = WorkflowTriggerRepository(db).create(
        workflow_id=workflow.id,
        trigger_type=WorkflowTriggerType.SCHEDULED,
        config={"cron": "0 0 1 1 *"},  # next occurrence: not due again for a long time
    )
    scheduler_jobs = SchedulerJobRepository(db)
    scheduler_jobs.upsert_for_trigger(
        trigger.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db.commit()

    first = SchedulerService(db).run_due()
    second = SchedulerService(db).run_due()

    assert first == 1
    assert second == 0
    executions = WorkflowExecutionRepository(db).list_for_workflow(workflow.id)
    assert len(executions) == 1


@requires_infra
def test_run_due_skips_a_trigger_with_no_published_version(db: Session) -> None:
    user = _provision_user(db)
    workflow = WorkflowRepository(db).create(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        name="Unpublished",
        description=None,
    )
    trigger = WorkflowTriggerRepository(db).create(
        workflow_id=workflow.id,
        trigger_type=WorkflowTriggerType.SCHEDULED,
        config={"cron": "* * * * *"},
    )
    scheduler_jobs = SchedulerJobRepository(db)
    scheduler_jobs.upsert_for_trigger(
        trigger.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db.commit()

    fired = SchedulerService(db).run_due()

    assert fired == 0
    assert WorkflowExecutionRepository(db).list_for_workflow(workflow.id) == []


@requires_infra
def test_each_scheduled_run_belongs_to_its_own_organization(db: Session) -> None:
    workflows = []
    for _ in range(2):
        user = _provision_user(db)
        workflow, _version = _provision_published_workflow(
            db, organization_id=user.organization_id, user_id=user.id
        )
        trigger = WorkflowTriggerRepository(db).create(
            workflow_id=workflow.id,
            trigger_type=WorkflowTriggerType.SCHEDULED,
            config={"cron": "* * * * *"},
        )
        SchedulerJobRepository(db).upsert_for_trigger(
            trigger.id, next_run_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        workflows.append(workflow)
    db.commit()

    SchedulerService(db).run_due()

    for workflow in workflows:
        [execution] = WorkflowExecutionRepository(db).list_for_workflow(workflow.id)
        assert execution.organization_id == workflow.organization_id
