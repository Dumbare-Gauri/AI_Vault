import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from workspace_testing import workspace_of
from app.application.auth_service import AuthService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import ConflictError, ValidationError, get_settings
from vault_shared.db.models import (
    ApprovalStatus,
    ConnectorProvider,
    ExecutionJobStatus,
    WorkflowNodeType,
    WorkflowPolicyEffect,
    WorkflowTriggerType,
)
from vault_shared.db.repositories import (
    ApprovalDecisionRepository,
    ApprovalRequestRepository,
    ConnectorCredentialsRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    StorageConnectorRepository,
    WorkflowExecutionRepository,
    WorkflowNodeExecutionRepository,
    WorkflowNodeRepository,
    WorkflowPolicyRepository,
    WorkflowRepository,
    WorkflowVersionRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE, ApprovalService
from vault_shared.security.encryption import encrypt_token


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
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI service containers.",
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
    google_user = GoogleUserInfo(
        sub=f"sub-{unique}", email=f"founder-{unique}@example.com",
        email_verified=True, name="Ada Founder", picture=None,
    )
    session = AuthService(db).complete_google_login(google_user=google_user, ip_address=None)
    return session.user


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id, provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id, account_email="founder@acme.com", workspace_domain="acme.com",
    )
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id, access_token_encrypted=encrypt_token("access-1"),
        refresh_token_encrypted=encrypt_token("refresh-1"), granted_scopes=DRIVE_WRITE_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return connector


def _provision_plan(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id, triggered_by="manual", triggered_by_user_id=None
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=organization_id, connector_id=workspace_of(db, organization_id).id, recommendation_job_id=job.id, rule_name="duplicate_files",
        category="storage_optimization", title="Duplicates found", description="test",
        confidence=0.9, estimated_impact="impact", impact_value=10.0, risk_level="low",
        suggested_action="Remove them.", related_departments=[], affected_file_ids=[],
        priority_score=50.0,
    )
    plan = ExecutionPlanRepository(db).create(
        organization_id=organization_id, recommendation_id=recommendation.id,
        created_by_user_id=user_id, target_provider="google_workspace",
        estimated_impact="1 file", estimated_storage_savings_bytes=100, risk_level="low",
        rollback_available=True, required_permissions=[DRIVE_WRITE_SCOPE],
    )
    db.commit()
    return plan


def _provision_workflow_execution(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    workflow = WorkflowRepository(db).create(
        organization_id=organization_id, created_by_user_id=user_id, name="Test", description=None
    )
    version = WorkflowVersionRepository(db).create(
        workflow_id=workflow.id, version_number=1, created_by_user_id=user_id
    )
    trigger_node = WorkflowNodeRepository(db).create(
        workflow_version_id=version.id, node_type=WorkflowNodeType.TRIGGER, name="Start",
        config={}, next_nodes={"default": "approval"},
    )
    approval_node = WorkflowNodeRepository(db).create(
        workflow_version_id=version.id, node_type=WorkflowNodeType.APPROVAL, name="Approve",
        config={}, next_nodes={},
    )
    db.commit()
    execution = WorkflowExecutionRepository(db).create(
        workflow_id=workflow.id, workflow_version_id=version.id, organization_id=organization_id,
        trigger_type=WorkflowTriggerType.MANUAL, trigger_context={}, triggered_by_user_id=user_id,
    )
    node_execution = WorkflowNodeExecutionRepository(db).create(
        workflow_execution_id=execution.id, workflow_node_id=approval_node.id
    )
    db.commit()
    return execution, node_execution, trigger_node


def _service(db: Session, *, enqueued_jobs: list, enqueued_workflows: list) -> ApprovalService:
    return ApprovalService(
        db,
        enqueue_execution_job=lambda job_id: enqueued_jobs.append(job_id),
        enqueue_workflow_execution=lambda exec_id: enqueued_workflows.append(exec_id),
    )


@requires_infra
def test_decide_on_a_plain_workflow_node_approval_resumes_the_workflow_without_a_job(
    db: Session,
) -> None:
    user = _provision_user(db)
    execution, node_execution, _trigger = _provision_workflow_execution(
        db, organization_id=user.organization_id, user_id=user.id
    )
    request = ApprovalRequestRepository(db).create(
        organization_id=user.organization_id, requested_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1), workflow_node_execution_id=node_execution.id,
    )
    db.commit()

    enqueued_jobs: list = []
    enqueued_workflows: list = []
    service = _service(db, enqueued_jobs=enqueued_jobs, enqueued_workflows=enqueued_workflows)

    decided = service.decide(
        request.id, organization_id=user.organization_id, user_id=user.id,
        decision="approve", comments=None, ip_address=None,
    )

    assert decided.status == ApprovalStatus.APPROVED
    assert enqueued_jobs == []
    assert enqueued_workflows == [execution.id]


@requires_infra
def test_decide_on_an_execute_action_approval_both_creates_a_job_and_resumes_the_workflow(
    db: Session,
) -> None:
    user = _provision_user(db)
    _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    execution, node_execution, _trigger = _provision_workflow_execution(
        db, organization_id=user.organization_id, user_id=user.id
    )
    request = ApprovalRequestRepository(db).create(
        organization_id=user.organization_id, requested_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        execution_plan_id=plan.id, workflow_node_execution_id=node_execution.id,
    )
    db.commit()

    enqueued_jobs: list = []
    enqueued_workflows: list = []
    service = _service(db, enqueued_jobs=enqueued_jobs, enqueued_workflows=enqueued_workflows)

    decided = service.decide(
        request.id, organization_id=user.organization_id, user_id=user.id,
        decision="approve", comments=None, ip_address=None,
    )

    assert decided.status == ApprovalStatus.APPROVED
    assert len(enqueued_jobs) == 1
    assert enqueued_workflows == [execution.id]

    jobs = ExecutionJobRepository(db).list_for_plan(plan.id)
    assert len(jobs) == 1
    assert jobs[0].status == ExecutionJobStatus.PENDING


@requires_infra
def test_auto_decide_via_policy_creates_a_policy_attributed_decision_and_job(db: Session) -> None:
    user = _provision_user(db)
    _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    execution, node_execution, _trigger = _provision_workflow_execution(
        db, organization_id=user.organization_id, user_id=user.id
    )
    request = ApprovalRequestRepository(db).create(
        organization_id=user.organization_id, requested_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        execution_plan_id=plan.id, workflow_node_execution_id=node_execution.id,
    )
    policy = WorkflowPolicyRepository(db).create_draft(
        organization_id=user.organization_id, policy_key="auto-archive", name="Auto archive",
        description=None, effect=WorkflowPolicyEffect.AUTO_EXECUTE, conditions={},
        created_by_user_id=user.id,
    )
    db.commit()

    enqueued_jobs: list = []
    enqueued_workflows: list = []
    service = _service(db, enqueued_jobs=enqueued_jobs, enqueued_workflows=enqueued_workflows)

    decided = service.auto_decide_via_policy(request.id, policy=policy)

    assert decided.status == ApprovalStatus.APPROVED
    assert len(enqueued_jobs) == 1
    assert enqueued_workflows == [execution.id]

    decisions = ApprovalDecisionRepository(db).list_for_request(request.id)
    assert len(decisions) == 1
    assert decisions[0].decided_by_policy_id == policy.id
    assert decisions[0].decider_user_id is None


@requires_infra
def test_auto_decide_via_policy_rejects_a_request_with_no_plan(db: Session) -> None:
    user = _provision_user(db)
    _execution, node_execution, _trigger = _provision_workflow_execution(
        db, organization_id=user.organization_id, user_id=user.id
    )
    request = ApprovalRequestRepository(db).create(
        organization_id=user.organization_id, requested_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1), workflow_node_execution_id=node_execution.id,
    )
    policy = WorkflowPolicyRepository(db).create_draft(
        organization_id=user.organization_id, policy_key="p", name="P", description=None,
        effect=WorkflowPolicyEffect.AUTO_EXECUTE, conditions={}, created_by_user_id=user.id,
    )
    db.commit()

    enqueued_jobs: list = []
    enqueued_workflows: list = []
    service = _service(db, enqueued_jobs=enqueued_jobs, enqueued_workflows=enqueued_workflows)

    with pytest.raises(ValidationError):
        service.auto_decide_via_policy(request.id, policy=policy)


@requires_infra
def test_decide_twice_on_the_same_request_raises_conflict(db: Session) -> None:
    user = _provision_user(db)
    _execution, node_execution, _trigger = _provision_workflow_execution(
        db, organization_id=user.organization_id, user_id=user.id
    )
    request = ApprovalRequestRepository(db).create(
        organization_id=user.organization_id, requested_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1), workflow_node_execution_id=node_execution.id,
    )
    db.commit()

    enqueued_jobs: list = []
    enqueued_workflows: list = []
    service = _service(db, enqueued_jobs=enqueued_jobs, enqueued_workflows=enqueued_workflows)
    service.decide(
        request.id, organization_id=user.organization_id, user_id=user.id,
        decision="reject", comments=None, ip_address=None,
    )

    with pytest.raises(ConflictError):
        service.decide(
            request.id, organization_id=user.organization_id, user_id=user.id,
            decision="approve", comments=None, ip_address=None,
        )
