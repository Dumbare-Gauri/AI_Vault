import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from workspace_testing import workspace_of
from sqlalchemy.orm import Session

from vault_shared import get_settings
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    NotificationChannel,
    RoleName,
    WorkflowExecutionStatus,
    WorkflowNodeExecutionStatus,
    WorkflowNodeType,
    WorkflowPolicyEffect,
    WorkflowTriggerType,
)
from vault_shared.db.repositories import (
    ApprovalRequestRepository,
    ConnectorCredentialsRepository,
    ExecutionJobRepository,
    FileRepository,
    NotificationRepository,
    OrganizationRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    RoleRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
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
from worker.workflow.execution_service import WorkflowExecutionService


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


def _provision_connector(
    db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID
):
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("access-1"),
        refresh_token_encrypted=encrypt_token("refresh-1"),
        granted_scopes=DRIVE_WRITE_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return connector


def _provision_file(db: Session, *, connector_id: uuid.UUID):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=f"f-{uuid.uuid4().hex[:8]}",
        provider_parent_id=None,
        parent_folder_id=None,
        name="Dup.txt",
        path="/Dup.txt",
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


def _provision_recommendation(
    db: Session,
    *,
    organization_id: uuid.UUID,
    rule_name: str = "duplicate_files",
    affected_file_ids: list[str] | None = None,
):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id,
        triggered_by="manual",
        triggered_by_user_id=None,
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=organization_id, connector_id=workspace_of(db, organization_id).id,
        recommendation_job_id=job.id,
        rule_name=rule_name,
        category="storage_optimization",
        title="Duplicates found",
        description="test",
        confidence=0.9,
        estimated_impact="impact",
        impact_value=10.0,
        risk_level="low",
        suggested_action="Remove them.",
        related_departments=[],
        affected_file_ids=affected_file_ids or [],
        priority_score=50.0,
    )
    db.commit()
    return recommendation


def _provision_workflow(
    db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID, nodes: list[dict]
):
    """`nodes` use the same client-key shape the API accepts — this helper
    translates keys to real ids itself, mirroring `WorkflowService.
    replace_nodes`, so worker tests don't need the backend service."""
    workflow = WorkflowRepository(db).create(
        organization_id=organization_id,
        created_by_user_id=user_id,
        name="Test",
        description=None,
    )
    version = WorkflowVersionRepository(db).create(
        workflow_id=workflow.id, version_number=1, created_by_user_id=user_id
    )
    node_repo = WorkflowNodeRepository(db)
    key_to_id: dict[str, uuid.UUID] = {}
    created = []
    for node in nodes:
        row = node_repo.create(
            workflow_version_id=version.id,
            node_type=node["node_type"],
            name=node.get("name", node["key"]),
            config=node.get("config", {}),
            next_nodes={},
        )
        key_to_id[node["key"]] = row.id
        created.append((node, row))
    for node, row in created:
        translated = {
            outcome: str(key_to_id[target])
            for outcome, target in node.get("next_nodes", {}).items()
        }
        node_repo.update_next_nodes(row, next_nodes=translated)
    db.commit()
    return workflow, version, {n["key"]: row for n, row in created}


def _provision_execution(
    db: Session, *, workflow, version, organization_id: uuid.UUID, user_id: uuid.UUID
):
    execution = WorkflowExecutionRepository(db).create(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        organization_id=organization_id,
        trigger_type=WorkflowTriggerType.MANUAL,
        trigger_context={},
        triggered_by_user_id=user_id,
    )
    db.commit()
    return execution


@requires_infra
def test_trigger_notification_end_completes_and_sends_a_real_in_app_notification(
    db: Session,
) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "n2"},
            },
            {
                "key": "n2",
                "node_type": WorkflowNodeType.NOTIFICATION,
                "config": {
                    "channel": NotificationChannel.IN_APP,
                    "recipients": ["owner"],
                    "subject": "Test",
                    "body_template": "Hello",
                },
                "next_nodes": {"default": "n3"},
            },
            {"key": "n3", "node_type": WorkflowNodeType.END},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    completed = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert completed.status == WorkflowExecutionStatus.COMPLETED
    assert completed.error is None

    notifications = NotificationRepository(db).list_for_user(
        user.id, organization_id=user.organization_id
    )
    assert len(notifications) == 1
    assert notifications[0].status == "sent"

    node_executions = WorkflowNodeExecutionRepository(db).list_for_execution(
        execution.id
    )
    assert all(
        ne.status == WorkflowNodeExecutionStatus.COMPLETED for ne in node_executions
    )
    assert len(node_executions) == 3


@requires_infra
def test_condition_node_branches_on_seeded_context(db: Session) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "cond"},
            },
            {
                "key": "cond",
                "node_type": WorkflowNodeType.CONDITION,
                "config": {"field": "file_count", "op": "gt", "value": 100},
                "next_nodes": {"true": "big", "false": "small"},
            },
            {"key": "big", "node_type": WorkflowNodeType.END, "name": "Big"},
            {"key": "small", "node_type": WorkflowNodeType.END, "name": "Small"},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )
    WorkflowExecutionRepository(db).set_context(execution, context={"file_count": 500})

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    completed = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert completed.status == WorkflowExecutionStatus.COMPLETED

    node_executions = WorkflowNodeExecutionRepository(db).list_for_execution(
        execution.id
    )
    executed_node_ids = {ne.workflow_node_id for ne in node_executions}
    assert nodes["big"].id in executed_node_ids
    assert nodes["small"].id not in executed_node_ids


@requires_infra
def test_ai_evaluation_node_calls_the_gateway_and_stores_context(db: Session) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "eval"},
            },
            {
                "key": "eval",
                "node_type": WorkflowNodeType.AI_EVALUATION,
                "config": {
                    "prompt_template": "Summarize this.",
                    "context_key": "summary",
                },
                "next_nodes": {"default": "end"},
            },
            {"key": "end", "node_type": WorkflowNodeType.END},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    completed = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert completed.status == WorkflowExecutionStatus.COMPLETED
    assert "summary" in completed.context

    eval_node_execution = next(
        ne
        for ne in WorkflowNodeExecutionRepository(db).list_for_execution(execution.id)
        if ne.workflow_node_id == nodes["eval"].id
    )
    assert "provider" in eval_node_execution.output_context


@requires_infra
def test_approval_node_pauses_then_resumes_on_the_correct_branch(db: Session) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "approval"},
            },
            {
                "key": "approval",
                "node_type": WorkflowNodeType.APPROVAL,
                "next_nodes": {"approved": "end_ok", "rejected": "end_no"},
            },
            {
                "key": "end_ok",
                "node_type": WorkflowNodeType.END,
                "name": "Approved end",
            },
            {
                "key": "end_no",
                "node_type": WorkflowNodeType.END,
                "name": "Rejected end",
            },
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    paused = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert paused.status == WorkflowExecutionStatus.PAUSED
    assert paused.current_node_id == nodes["approval"].id

    node_execution = WorkflowNodeExecutionRepository(db).get_for_execution_and_node(
        workflow_execution_id=execution.id, workflow_node_id=nodes["approval"].id
    )
    assert node_execution.status == WorkflowNodeExecutionStatus.WAITING_APPROVAL
    approval_request = ApprovalRequestRepository(db).get_by_workflow_node_execution(
        node_execution.id
    )
    assert approval_request is not None

    resumed_calls: list = []
    approval_service = ApprovalService(
        db,
        enqueue_execution_job=lambda job_id: None,
        enqueue_workflow_execution=lambda exec_id: resumed_calls.append(exec_id),
    )
    approval_service.decide(
        approval_request.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="approve",
        comments=None,
        ip_address=None,
    )
    assert resumed_calls == [execution.id]

    # Simulates the worker picking the resume task back up.
    service.run(execution.id)

    completed = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert completed.status == WorkflowExecutionStatus.COMPLETED

    executed_node_ids = {
        ne.workflow_node_id
        for ne in WorkflowNodeExecutionRepository(db).list_for_execution(execution.id)
    }
    assert nodes["end_ok"].id in executed_node_ids
    assert nodes["end_no"].id not in executed_node_ids


@requires_infra
def test_execute_action_node_is_skipped_when_no_policy_is_configured(
    db: Session,
) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "act"},
            },
            {
                "key": "act",
                "node_type": WorkflowNodeType.EXECUTE_ACTION,
                "config": {
                    "rule_name": "duplicate_files",
                    "policy_key": "missing-policy",
                },
                "next_nodes": {"default": "end"},
            },
            {"key": "end", "node_type": WorkflowNodeType.END},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    completed = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert completed.status == WorkflowExecutionStatus.COMPLETED

    act_node_execution = next(
        ne
        for ne in WorkflowNodeExecutionRepository(db).list_for_execution(execution.id)
        if ne.workflow_node_id == nodes["act"].id
    )
    assert act_node_execution.status == WorkflowNodeExecutionStatus.SKIPPED


@requires_infra
def test_a_policy_cannot_auto_execute_a_removal_it_waits_for_a_person(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id
    )
    file = _provision_file(db, connector_id=connector.id)
    _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[str(file.id)],
    )
    policy = WorkflowPolicyRepository(db).create_draft(
        organization_id=user.organization_id,
        policy_key="auto-dup",
        name="Auto dup",
        description=None,
        effect=WorkflowPolicyEffect.AUTO_EXECUTE,
        conditions={},
        created_by_user_id=user.id,
    )
    WorkflowPolicyRepository(db).publish(policy)
    db.commit()

    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "act"},
            },
            {
                "key": "act",
                "node_type": WorkflowNodeType.EXECUTE_ACTION,
                "config": {"rule_name": "duplicate_files", "policy_key": "auto-dup"},
                "next_nodes": {"default": "end"},
            },
            {"key": "end", "node_type": WorkflowNodeType.END},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    service = WorkflowExecutionService(db)
    service.run(execution.id)

    paused = WorkflowExecutionRepository(db).get_by_id(execution.id)
    assert paused.status == WorkflowExecutionStatus.PAUSED

    act_node_execution = next(
        ne
        for ne in WorkflowNodeExecutionRepository(db).list_for_execution(execution.id)
        if ne.workflow_node_id == nodes["act"].id
    )
    assert act_node_execution.status == WorkflowNodeExecutionStatus.WAITING_APPROVAL
    assert ExecutionJobRepository(db).list_for_organization(user.organization_id) == []


@requires_infra
def test_cancellation_stops_a_paused_execution_on_resume(db: Session) -> None:
    user = _provision_user(db)
    workflow, version, nodes = _provision_workflow(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        nodes=[
            {
                "key": "n1",
                "node_type": WorkflowNodeType.TRIGGER,
                "next_nodes": {"default": "approval"},
            },
            {
                "key": "approval",
                "node_type": WorkflowNodeType.APPROVAL,
                "next_nodes": {"default": "end"},
            },
            {"key": "end", "node_type": WorkflowNodeType.END},
        ],
    )
    execution = _provision_execution(
        db,
        workflow=workflow,
        version=version,
        organization_id=user.organization_id,
        user_id=user.id,
    )
    service = WorkflowExecutionService(db)
    service.run(execution.id)

    executions_repo = WorkflowExecutionRepository(db)
    paused = executions_repo.get_by_id(execution.id)
    assert paused.status == WorkflowExecutionStatus.PAUSED

    executions_repo.request_cancel(paused)
    db.commit()

    service.run(execution.id)

    cancelled = executions_repo.get_by_id(execution.id)
    assert cancelled.status == WorkflowExecutionStatus.CANCELLED
