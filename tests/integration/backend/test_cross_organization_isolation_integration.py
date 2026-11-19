"""Cross-organization isolation, end to end through the real HTTP stack.

Two tenants are seeded with a full set of records. Requests are made as an
owner of organization A — with a genuine signed access token, through the real
auth dependency, against the real database — using organization B's
identifiers. For every resource type:

* A direct ID request for B's record is indistinguishable from a request for
  a record that does not exist (same status, same body): B's existence is
  not revealed.
* Mutating requests change nothing in B.
* Listings, searches and filters never return B's data, however they are
  parameterized.
* Forged identity claims do not widen access.

Only the AI gateway's embedding provider is replaced (a fixed vector, so no
model download is needed); nothing about tenant scoping is faked.
"""

import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.application.auth_service import AuthService
from app.application.execution_plan_service import ExecutionPlanService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from app.infrastructure.auth.jwt import AccessTokenClaims, create_access_token
from app.main import app
from vault_shared import get_settings
from vault_shared.ai_gateway import AIGateway, get_ai_gateway
from vault_shared.ai_gateway.interfaces import EmbeddingResult
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider
from vault_shared.db.models import (
    ApprovalStatus,
    ConnectorProvider,
    DriveType,
    ExecutionJobStatus,
    StorageAnalysisTrigger,
)
from vault_shared.db.repositories import (
    ApprovalRequestRepository,
    ArchiveJobRepository,
    ConnectorCredentialsRepository,
    ConversationMessageRepository,
    ConversationRepository,
    DuplicateGroupRepository,
    EmbeddingJobRepository,
    EmbeddingRepository,
    EnrichmentJobRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionStepRepository,
    FileExtractionRepository,
    FileRepository,
    IntelligenceJobRepository,
    NotificationRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    RollbackRecordRepository,
    ScanJobRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
    WorkflowExecutionRepository,
    WorkflowPolicyRepository,
    WorkflowRepository,
    WorkflowTriggerRepository,
    WorkflowVersionRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
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
    reason=(
        "Postgres/Redis not reachable — run against `docker compose up` or CI service containers."
    ),
)

_QUERY_VECTOR = [1.0, 0.0, 0.0, 0.0]


class _FixedVectorEmbeddingProvider:
    name = "fixed_vector_test_double"
    model_name = "test"
    model_version = "1"

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [
            EmbeddingResult(
                vector=_QUERY_VECTOR, model_name="test", model_version="1", dimensions=4
            )
            for _ in texts
        ]


@dataclass
class Tenant:
    label: str
    user: object
    organization_id: uuid.UUID
    token: str
    ids: dict[str, str] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)
    total_files: int = 0

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _token(user) -> str:
    return create_access_token(
        AccessTokenClaims(
            user_id=user.id, organization_id=user.organization_id, role=user.role.name
        )
    )


def _provision_user(db: Session, label: str):
    unique = uuid.uuid4().hex[:12]
    session = AuthService(db).complete_google_login(
        google_user=GoogleUserInfo(
            sub=f"sub-{unique}",
            email=f"{label.lower()}-{unique}@{label.lower()}-corp.example",
            email_verified=True,
            name=f"{label} Founder",
            picture=None,
        ),
        ip_address=None,
    )
    return session.user


def _add_file(
    db: Session,
    *,
    source_id: uuid.UUID,
    name: str,
    mime_type: str = "application/pdf",
    size_bytes: int = 4096,
    checksum: str | None = None,
    modified: datetime | None = None,
):
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source_id,
        provider_file_id=f"drive-{uuid.uuid4().hex[:12]}",
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type=mime_type,
        size_bytes=size_bytes,
        owner_email="owner@example.com",
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=checksum,
        web_view_link=None,
        provider_created_at=modified or now,
        provider_modified_at=modified or now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    db.flush()
    return file


def _seed_tenant(db: Session, label: str, *, total_files: int) -> Tenant:
    """Every record type an endpoint can address, named with a per-tenant
    marker so leaks are detectable by simple text search."""
    user = _provision_user(db, label)
    org_id = user.organization_id
    tenant = Tenant(
        label=label, user=user, organization_id=org_id, token=_token(user), total_files=total_files
    )
    ids = tenant.ids
    ids["organization_id"] = str(org_id)
    ids["user_id"] = str(user.id)

    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=org_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user.id,
        account_email=f"drive-owner@{label.lower()}-corp.example",
        workspace_domain=f"{label.lower()}-corp.example",
    )
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("access"),
        refresh_token_encrypted=encrypt_token("refresh"),
        granted_scopes=DRIVE_WRITE_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    ids["connector_id"] = str(connector.id)

    source = StorageSourceRepository(db).upsert(
        connector_id=connector.id,
        provider_drive_id="root",
        name=f"{label}-Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    secret_name = f"{label}-Confidential-Payroll-Ledger.pdf"
    file = _add_file(db, source_id=source.id, name=secret_name, size_bytes=9_000_000)
    duplicate = _add_file(
        db,
        source_id=source.id,
        name=f"{label}-Confidential-Payroll-Ledger-copy.pdf",
        size_bytes=9_000_000,
    )
    folder = _add_file(
        db,
        source_id=source.id,
        name=f"{label}-Secret-Folder",
        mime_type="application/vnd.google-apps.folder",
        size_bytes=0,
    )
    trashed = _add_file(db, source_id=source.id, name=f"{label}-Trashed-Draft.pdf")
    FileRepository(db).mark_trashed(trashed, trashed=True)
    ids["file_id"] = str(file.id)
    ids["duplicate_file_id"] = str(duplicate.id)
    ids["folder_id"] = str(folder.id)
    ids["trashed_file_id"] = str(trashed.id)
    ids["file_name"] = secret_name

    now = datetime.now(UTC)
    body = f"{label} confidential payroll figures and salary bands."
    FileExtractionRepository(db).upsert(
        file_id=file.id,
        status="success",
        extractor_name="pdf_text",
        extracted_text=body,
        char_count=len(body),
        error=None,
        extracted_at=now,
    )
    # A second file with a distinct vector: a lone candidate identical to
    # the query is a degenerate case for the similarity ranking's
    # mean-centering, which would make every search look empty.
    for embedded, vector, text in (
        (file, _QUERY_VECTOR, body),
        (duplicate, [0.0, 1.0, 0.0, 0.0], f"{label} unrelated quarterly meeting notes."),
    ):
        if embedded is not file:
            FileExtractionRepository(db).upsert(
                file_id=embedded.id,
                status="success",
                extractor_name="pdf_text",
                extracted_text=text,
                char_count=len(text),
                error=None,
                extracted_at=now,
            )
        EmbeddingRepository(db).upsert(
            file_id=embedded.id,
            model_name="test",
            model_version="1",
            dimensions=4,
            vector=vector,
            content_hash=f"hash-{embedded.id}",
            embedded_at=now,
        )

    ids["scan_job_id"] = str(
        ScanJobRepository(db)
        .create(connector_id=connector.id, scan_type="full", triggered_by_user_id=user.id)
        .id
    )
    ids["enrichment_job_id"] = str(
        EnrichmentJobRepository(db)
        .create(connector_id=connector.id, triggered_by="manual", triggered_by_user_id=user.id)
        .id
    )
    ids["embedding_job_id"] = str(
        EmbeddingJobRepository(db)
        .create(connector_id=connector.id, triggered_by="manual", triggered_by_user_id=user.id)
        .id
    )
    ids["intelligence_job_id"] = str(
        IntelligenceJobRepository(db)
        .create(connector_id=connector.id, triggered_by="manual", triggered_by_user_id=user.id)
        .id
    )

    recommendation_job = RecommendationJobRepository(db).create(
        organization_id=org_id, triggered_by="manual", triggered_by_user_id=None
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=org_id,
        recommendation_job_id=recommendation_job.id,
        rule_name="duplicate_files",
        category="storage_optimization",
        title=f"{label}-Recommendation-Consolidate-Payroll",
        description=f"{label} description",
        confidence=0.9,
        estimated_impact="impact",
        impact_value=10.0,
        risk_level="low",
        suggested_action="Consolidate.",
        related_departments=[],
        affected_file_ids=[str(file.id)],
        priority_score=50.0,
    )
    ids["recommendation_id"] = str(recommendation.id)
    ids["recommendation_title"] = recommendation.title

    db.commit()

    plan = ExecutionPlanService(db).create_plan(
        recommendation.id, organization_id=org_id, user_id=user.id
    )
    ids["execution_plan_id"] = str(plan.id)
    approval = ApprovalRequestRepository(db).get_by_plan(plan.id)
    ids["approval_request_id"] = str(approval.id)
    step = ExecutionStepRepository(db).list_for_plan(plan.id)[0]
    ids["execution_step_id"] = str(step.id)
    RollbackRecordRepository(db).create(execution_step_id=step.id, pre_state={"trashed": False})
    job = ExecutionJobRepository(db).create(
        execution_plan_id=plan.id, organization_id=org_id, triggered_by_user_id=user.id
    )
    ids["execution_job_id"] = str(job.id)

    archive_plan = ExecutionPlanRepository(db).create(
        organization_id=org_id,
        recommendation_id=None,
        duplicate_group_id=None,
        created_by_user_id=user.id,
        target_provider="google_workspace",
        estimated_impact="archive",
        estimated_storage_savings_bytes=None,
        risk_level="low",
        rollback_available=True,
        required_permissions=[],
    )
    db.flush()
    archive = ArchiveJobRepository(db).create(
        organization_id=org_id,
        execution_plan_id=archive_plan.id,
        name=f"{label}-Archive-Of-Payroll",
        created_by_user_id=user.id,
    )
    ArchiveJobRepository(db).mark_completed(
        archive,
        object_storage_key=f"archives/{org_id}/{archive_plan.id}.zip",
        original_size_bytes=100,
        compressed_size_bytes=50,
        file_count=1,
        manifest=[
            {
                "file_id": str(file.id),
                "name": secret_name,
                "path": f"/{secret_name}",
                "size_bytes": 100,
                "mime_type": "application/pdf",
                "checksum_sha256": "abc",
            }
        ],
    )
    ids["archive_id"] = str(archive.id)
    ids["archive_name"] = archive.name

    analysis_job = StorageAnalysisJobRepository(db).create(
        organization_id=org_id,
        triggered_by=StorageAnalysisTrigger.MANUAL,
        triggered_by_user_id=None,
    )
    StorageAnalysisJobRepository(db).mark_completed(analysis_job)
    group = DuplicateGroupRepository(db).upsert_group(
        organization_id=org_id,
        storage_analysis_job_id=analysis_job.id,
        checksum=f"{label}-dup-checksum",
        file_count=2,
        total_size_bytes=18_000_000,
        recoverable_size_bytes=9_000_000,
        recommended_keep_file_id=file.id,
        recommended_keep_reason="Keep the original.",
        recommended_keep_confidence=0.9,
    )
    DuplicateGroupRepository(db).replace_members(group, [(file.id, True), (duplicate.id, False)])
    ids["duplicate_group_id"] = str(group.id)
    StorageAnalysisSnapshotRepository(db).create(
        organization_id=org_id,
        storage_analysis_job_id=analysis_job.id,
        total_size_bytes=10_240,
        total_files=total_files,
        total_folders=1,
        breakdown_by_type_bytes={},
        breakdown_by_size_bucket_bytes={},
        breakdown_by_source_bytes={},
        duplicate_group_count=1,
        duplicate_file_count=2,
        duplicate_recoverable_bytes=9_000_000,
        large_file_count=2,
        large_file_bytes=18_000_000,
        old_file_count=0,
        old_file_bytes=0,
        inactive_file_count=0,
        inactive_file_bytes=0,
        temporary_candidate_count=0,
        temporary_candidate_bytes=0,
        total_potential_savings_bytes=9_000_000,
    )

    conversation = ConversationRepository(db).create(
        organization_id=org_id, user_id=user.id, title=f"{label}-Private-Conversation"
    )
    ConversationMessageRepository(db).create(
        conversation_id=conversation.id, role="user", content=f"{label} private question"
    )
    ids["conversation_id"] = str(conversation.id)
    ids["conversation_title"] = conversation.title

    workflow = WorkflowRepository(db).create(
        organization_id=org_id,
        created_by_user_id=user.id,
        name=f"{label}-Secret-Workflow",
        description=None,
    )
    version = WorkflowVersionRepository(db).create(
        workflow_id=workflow.id, version_number=1, created_by_user_id=user.id
    )
    trigger = WorkflowTriggerRepository(db).create(
        workflow_id=workflow.id, trigger_type="manual", config={}
    )
    execution = WorkflowExecutionRepository(db).create(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        organization_id=org_id,
        trigger_type="manual",
        trigger_context={},
        triggered_by_user_id=user.id,
    )
    ids["workflow_id"] = str(workflow.id)
    ids["workflow_name"] = workflow.name
    ids["workflow_version_id"] = str(version.id)
    ids["workflow_trigger_id"] = str(trigger.id)
    ids["workflow_execution_id"] = str(execution.id)

    policy = WorkflowPolicyRepository(db).create_draft(
        organization_id=org_id,
        policy_key=f"{label.lower()}-secret-policy",
        name=f"{label}-Secret-Policy",
        description=None,
        effect="require_approval",
        conditions={},
        created_by_user_id=user.id,
    )
    ids["workflow_policy_id"] = str(policy.id)
    ids["policy_key"] = policy.policy_key
    ids["policy_name"] = policy.name

    notification = NotificationRepository(db).create(
        organization_id=org_id,
        user_id=user.id,
        channel="in_app",
        subject=f"{label}-Private-Notification",
        body=f"{label} body",
    )
    ids["notification_subject"] = notification.subject

    db.commit()

    tenant.markers = [
        f"{label}-Confidential",
        f"{label}-Secret",
        f"{label}-Trashed",
        f"{label}-Recommendation",
        f"{label}-Archive",
        f"{label}-Private",
        f"{label}-Drive",
        f"{label.lower()}-corp.example",
        f"{label.lower()}-secret-policy",
        *(value for key, value in ids.items() if key.endswith("_id") and key != "user_id"),
        str(user.id),
    ]
    return tenant


@pytest.fixture(scope="module")
def tenants():
    session = get_session_factory()()
    try:
        yield (
            _seed_tenant(session, "Atlas", total_files=111),
            _seed_tenant(session, "Borealis", total_files=222),
        )
    finally:
        session.close()


@pytest.fixture(scope="module")
def client():
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider(),
        completion_provider=ExtractiveCompletionProvider(),
    )
    app.dependency_overrides[get_ai_gateway] = lambda: gateway
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_ai_gateway, None)


def _fresh_ids(template: str) -> dict[str, str]:
    return {
        name: str(uuid.uuid4())
        for name in (
            "connector_id",
            "file_id",
            "scan_job_id",
            "enrichment_job_id",
            "embedding_job_id",
            "intelligence_job_id",
            "recommendation_id",
            "execution_plan_id",
            "execution_job_id",
            "approval_request_id",
            "archive_id",
            "conversation_id",
            "duplicate_group_id",
            "workflow_id",
            "workflow_version_id",
            "workflow_trigger_id",
            "workflow_execution_id",
            "workflow_policy_id",
        )
    } | {"policy_key": f"missing-{uuid.uuid4().hex[:8]}", "group_id": str(uuid.uuid4())}


def _path_values(ids: dict[str, str]) -> dict[str, str]:
    values = dict(ids)
    values["group_id"] = ids["duplicate_group_id"]
    values["embedding_job_id"] = ids["embedding_job_id"]
    return values


def _normalize(body: object, ids: set[str]) -> object:
    text = repr(body)
    for value in ids:
        text = text.replace(value, "<id>")
    return text


_QUESTION = {"question": "hello"}

# (name, method, path template, json body)
DIRECT_ID_REQUESTS = [
    ("connector status", "GET", "/v1/connectors/{connector_id}/status", None),
    ("connector files", "GET", "/v1/connectors/{connector_id}/files?ownership=all&limit=500", None),
    ("connector folders", "GET", "/v1/connectors/{connector_id}/folders?query=Secret", None),
    ("connector trash", "GET", "/v1/connectors/{connector_id}/trash", None),
    ("connector scans", "GET", "/v1/connectors/{connector_id}/scans", None),
    ("connector enrichment", "GET", "/v1/connectors/{connector_id}/enrichment", None),
    ("connector embedding", "GET", "/v1/connectors/{connector_id}/embedding", None),
    ("connector intelligence", "GET", "/v1/connectors/{connector_id}/intelligence", None),
    ("start scan", "POST", "/v1/connectors/{connector_id}/scans", {}),
    ("start enrichment", "POST", "/v1/connectors/{connector_id}/enrichment", {}),
    ("start embedding", "POST", "/v1/connectors/{connector_id}/embedding", {}),
    ("start intelligence", "POST", "/v1/connectors/{connector_id}/intelligence", {}),
    ("verify connector", "POST", "/v1/connectors/{connector_id}/verify", {}),
    ("disconnect connector", "POST", "/v1/connectors/{connector_id}/disconnect", {}),
    ("file detail", "GET", "/v1/files/{file_id}", None),
    ("file download", "GET", "/v1/files/{file_id}/download", None),
    ("scan job", "GET", "/v1/scans/{scan_job_id}", None),
    ("cancel scan", "POST", "/v1/scans/{scan_job_id}/cancel", {}),
    ("enrichment job", "GET", "/v1/enrichment/{enrichment_job_id}", None),
    ("cancel enrichment", "POST", "/v1/enrichment/{enrichment_job_id}/cancel", {}),
    ("embedding job", "GET", "/v1/embedding/{embedding_job_id}", None),
    ("cancel embedding", "POST", "/v1/embedding/{embedding_job_id}/cancel", {}),
    ("intelligence job", "GET", "/v1/intelligence/{intelligence_job_id}", None),
    ("cancel intelligence", "POST", "/v1/intelligence/{intelligence_job_id}/cancel", {}),
    ("recommendation", "GET", "/v1/recommendations/{recommendation_id}", None),
    ("execution plan", "GET", "/v1/execution-plans/{execution_plan_id}", None),
    ("rollback plan", "POST", "/v1/execution-plans/{execution_plan_id}/rollback", {}),
    ("execution job", "GET", "/v1/execution-jobs/{execution_job_id}", None),
    ("cancel execution job", "POST", "/v1/execution-jobs/{execution_job_id}/cancel", {}),
    ("pause execution job", "POST", "/v1/execution-jobs/{execution_job_id}/pause", {}),
    ("resume execution job", "POST", "/v1/execution-jobs/{execution_job_id}/resume", {}),
    ("approval", "GET", "/v1/approvals/{approval_request_id}", None),
    (
        "decide approval",
        "POST",
        "/v1/approvals/{approval_request_id}/decide",
        {"decision": "approve"},
    ),
    ("archive", "GET", "/v1/archives/{archive_id}", None),
    ("archive download", "GET", "/v1/archives/{archive_id}/download", None),
    ("delete archive", "DELETE", "/v1/archives/{archive_id}", None),
    ("conversation", "GET", "/v1/conversations/{conversation_id}", None),
    ("ask in conversation", "POST", "/v1/conversations/{conversation_id}/messages", _QUESTION),
    ("duplicate group", "GET", "/v1/storage/duplicates/{group_id}", None),
    ("workflow", "GET", "/v1/workflows/{workflow_id}", None),
    ("workflow versions", "GET", "/v1/workflows/{workflow_id}/versions", None),
    ("workflow triggers", "GET", "/v1/workflows/{workflow_id}/triggers", None),
    ("workflow executions", "GET", "/v1/workflows/{workflow_id}/executions", None),
    ("run workflow", "POST", "/v1/workflows/{workflow_id}/executions", {}),
    ("clone workflow", "POST", "/v1/workflows/{workflow_id}/clone", {}),
    ("publish workflow", "POST", "/v1/workflows/{workflow_id}/publish", {}),
    ("set workflow status", "POST", "/v1/workflows/{workflow_id}/status", {"status": "paused"}),
    (
        "add workflow trigger",
        "POST",
        "/v1/workflows/{workflow_id}/triggers",
        {"trigger_type": "manual", "config": {}},
    ),
    (
        "toggle workflow trigger",
        "POST",
        "/v1/workflows/{workflow_id}/triggers/{workflow_trigger_id}/enabled",
        {"enabled": False},
    ),
    ("workflow execution", "GET", "/v1/workflow-executions/{workflow_execution_id}", None),
    (
        "cancel workflow execution",
        "POST",
        "/v1/workflow-executions/{workflow_execution_id}/cancel",
        {},
    ),
    (
        "pause workflow execution",
        "POST",
        "/v1/workflow-executions/{workflow_execution_id}/pause",
        {},
    ),
    (
        "resume workflow execution",
        "POST",
        "/v1/workflow-executions/{workflow_execution_id}/resume",
        {},
    ),
    ("workflow policy", "GET", "/v1/workflow-policies/{workflow_policy_id}", None),
    ("publish policy", "POST", "/v1/workflow-policies/{workflow_policy_id}/publish", {}),
    ("archive policy", "POST", "/v1/workflow-policies/{workflow_policy_id}/archive", {}),
    ("policy versions by key", "GET", "/v1/workflow-policies/by-key/{policy_key}/versions", None),
]


def _call(client: TestClient, method: str, url: str, headers: dict, body):
    return client.request(method, url, headers=headers, json=body)


class TestDirectIdAccessRevealsNothing:
    @requires_infra
    @pytest.mark.parametrize(
        ("name", "method", "template", "body"),
        DIRECT_ID_REQUESTS,
        ids=[case[0] for case in DIRECT_ID_REQUESTS],
    )
    def test_a_foreign_id_is_indistinguishable_from_a_missing_one(
        self, client: TestClient, tenants, name: str, method: str, template: str, body
    ) -> None:
        attacker, victim = tenants
        foreign_ids = _path_values(victim.ids)
        missing_ids = _fresh_ids(template)
        missing_ids["group_id"] = str(uuid.uuid4())

        foreign = _call(client, method, template.format(**foreign_ids), attacker.headers, body)
        missing = _call(client, method, template.format(**missing_ids), attacker.headers, body)

        assert foreign.status_code == missing.status_code, (
            f"{name}: foreign id answered {foreign.status_code}, "
            f"a nonexistent id answers {missing.status_code}"
        )
        assert _normalize(
            foreign.json() if foreign.content else None, set(foreign_ids.values())
        ) == (_normalize(missing.json() if missing.content else None, set(missing_ids.values())))
        assert foreign.status_code != 200 or method == "GET" and "by-key" in template

    @requires_infra
    @pytest.mark.parametrize(
        ("name", "method", "template", "body"),
        DIRECT_ID_REQUESTS,
        ids=[case[0] for case in DIRECT_ID_REQUESTS],
    )
    def test_a_foreign_id_response_never_contains_the_other_tenants_data(
        self, client: TestClient, tenants, name: str, method: str, template: str, body
    ) -> None:
        attacker, victim = tenants

        response = _call(
            client,
            method,
            template.format(**_path_values(victim.ids)),
            attacker.headers,
            body,
        )

        leaked = [marker for marker in victim.markers if marker in response.text]
        assert leaked == []


class TestForeignRequestsDoNotChangeTheOtherTenant:
    @requires_infra
    def test_no_mutation_by_the_attacker_altered_the_victims_records(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants
        for _name, method, template, body in DIRECT_ID_REQUESTS:
            if method != "GET":
                _call(
                    client,
                    method,
                    template.format(**_path_values(victim.ids)),
                    attacker.headers,
                    body,
                )

        session = get_session_factory()()
        try:
            job = ExecutionJobRepository(session).get_by_id(
                uuid.UUID(victim.ids["execution_job_id"])
            )
            assert job.status == ExecutionJobStatus.PENDING
            assert job.cancel_requested is False
            assert job.pause_requested is False
            approval = ApprovalRequestRepository(session).get_by_id(
                uuid.UUID(victim.ids["approval_request_id"])
            )
            assert approval.status == ApprovalStatus.PENDING
            archive = ArchiveJobRepository(session).get_by_id(uuid.UUID(victim.ids["archive_id"]))
            assert archive.status != "deleted"
            connector = StorageConnectorRepository(session).get_by_id(
                uuid.UUID(victim.ids["connector_id"])
            )
            assert connector.status == "connected"
            file = FileRepository(session).get_by_id(uuid.UUID(victim.ids["file_id"]))
            assert file.trashed is False
            assert (
                RollbackRecordRepository(session)
                .get_by_step(uuid.UUID(victim.ids["execution_step_id"]))
                .rolled_back
                is False
            )
            for repo, key in (
                (ScanJobRepository(session), "scan_job_id"),
                (EnrichmentJobRepository(session), "enrichment_job_id"),
                (EmbeddingJobRepository(session), "embedding_job_id"),
                (IntelligenceJobRepository(session), "intelligence_job_id"),
            ):
                assert repo.get_by_id(uuid.UUID(victim.ids[key])).cancel_requested is False
        finally:
            session.close()

    @requires_infra
    def test_the_attacker_started_no_jobs_on_the_victims_connector(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants
        for kind in ("scans", "enrichment", "embedding", "intelligence"):
            client.post(
                f"/v1/connectors/{victim.ids['connector_id']}/{kind}",
                headers=attacker.headers,
                json={},
            )

        session = get_session_factory()()
        try:
            connector_id = uuid.UUID(victim.ids["connector_id"])
            assert len(ScanJobRepository(session).list_for_connector(connector_id)) == 1
        finally:
            session.close()


class TestPlanCreationWithForeignReferences:
    @requires_infra
    @pytest.mark.parametrize(
        "body",
        [
            {"file_ids": ["{file_id}"], "action_type": "archive"},
            {"file_ids": ["{file_id}"], "action_type": "remove_duplicate"},
            {"file_ids": ["{file_id}"], "action_type": "create_archive"},
            {"file_ids": ["{file_id}"], "action_type": "rename", "new_name": "stolen.pdf"},
            {"file_ids": ["{file_id}"], "action_type": "move_file", "new_parent_id": "x"},
            {"file_ids": ["{file_id}", "{own_file_id}"], "action_type": "archive"},
            {"recommendation_id": "{recommendation_id}"},
            {"duplicate_group_id": "{duplicate_group_id}"},
        ],
        ids=[
            "archive",
            "remove-duplicate",
            "create-archive",
            "rename",
            "move",
            "mixed-own-and-foreign",
            "foreign-recommendation",
            "foreign-duplicate-group",
        ],
    )
    def test_a_plan_cannot_be_created_from_another_tenants_records(
        self, client: TestClient, tenants, body: dict
    ) -> None:
        attacker, victim = tenants
        values = {**victim.ids, "own_file_id": attacker.ids["file_id"]}

        def fill(value):
            if isinstance(value, list):
                return [fill(item) for item in value]
            if isinstance(value, str):
                return value.format(**values)
            return value

        request = {key: fill(value) for key, value in body.items()}

        def plan_ids(organization_id: uuid.UUID) -> set[str]:
            session = get_session_factory()()
            try:
                return {
                    str(plan.id)
                    for plan in ExecutionPlanRepository(session).list_for_organization(
                        organization_id
                    )
                }
            finally:
                session.close()

        attacker_before = plan_ids(attacker.organization_id)
        victim_before = plan_ids(victim.organization_id)

        response = client.post("/v1/execution-plans", headers=attacker.headers, json=request)

        assert response.status_code in (404, 422)
        assert not [marker for marker in victim.markers if marker in response.text]
        assert plan_ids(attacker.organization_id) == attacker_before
        assert plan_ids(victim.organization_id) == victim_before

    @requires_infra
    def test_a_permanent_delete_plan_cannot_target_another_tenants_file(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants

        response = client.post(
            "/v1/execution-plans/permanent-delete",
            headers=attacker.headers,
            json={"file_ids": [victim.ids["trashed_file_id"]]},
        )

        assert response.status_code in (404, 422)
        assert not [marker for marker in victim.markers if marker in response.text]

    @requires_infra
    def test_bulk_approval_ignores_a_foreign_request_id(self, client: TestClient, tenants) -> None:
        attacker, victim = tenants

        response = client.post(
            "/v1/approvals/bulk-decide",
            headers=attacker.headers,
            json={
                "approval_request_ids": [victim.ids["approval_request_id"]],
                "decision": "approve",
            },
        )

        assert not [marker for marker in victim.markers if marker in response.text]
        session = get_session_factory()()
        try:
            approval = ApprovalRequestRepository(session).get_by_id(
                uuid.UUID(victim.ids["approval_request_id"])
            )
            assert approval.status == ApprovalStatus.PENDING
        finally:
            session.close()


class TestNestedResourcesCannotBeCrossed:
    @requires_infra
    def test_a_workflow_version_cannot_be_reached_through_the_attackers_own_workflow(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants
        own_workflow = f"/v1/workflows/{attacker.ids['workflow_id']}/versions"
        mixed = f"{own_workflow}/{victim.ids['workflow_version_id']}"
        missing = f"{own_workflow}/{uuid.uuid4()}"

        for suffix, method, body in (
            ("/nodes", "PUT", {"nodes": []}),
            ("/rollback", "POST", {}),
        ):
            foreign = client.request(method, mixed + suffix, headers=attacker.headers, json=body)
            absent = client.request(method, missing + suffix, headers=attacker.headers, json=body)
            assert foreign.status_code == absent.status_code
            assert foreign.status_code in (404, 422)

    @requires_infra
    def test_a_workflow_trigger_cannot_be_toggled_through_the_attackers_own_workflow(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants

        response = client.post(
            f"/v1/workflows/{attacker.ids['workflow_id']}/triggers/"
            f"{victim.ids['workflow_trigger_id']}/enabled",
            headers=attacker.headers,
            json={"enabled": False},
        )

        assert response.status_code == 404
        session = get_session_factory()()
        try:
            trigger = WorkflowTriggerRepository(session).get_by_id(
                uuid.UUID(victim.ids["workflow_trigger_id"])
            )
            assert trigger.enabled is True
        finally:
            session.close()


LISTINGS = [
    "/v1/connectors",
    "/v1/approvals",
    "/v1/archives",
    "/v1/conversations",
    "/v1/execution-jobs",
    "/v1/execution-plans",
    "/v1/recommendations",
    "/v1/workflows",
    "/v1/workflow-executions",
    "/v1/workflow-policies",
    "/v1/notifications",
    "/v1/dashboard",
    "/v1/storage/overview",
    "/v1/storage/statistics",
    "/v1/storage/duplicates",
    "/v1/storage/candidates",
    "/v1/storage/large-files?min_size_bytes=1",
    "/v1/storage/old-files?older_than_days=1",
    "/v1/storage/inactive-files?inactive_days=1",
    "/v1/users/me",
    "/v1/organizations/current",
    "/v1/organizations/current/ai-provider",
]


class TestListingsAndFiltersNeverReturnAnotherTenantsData:
    @requires_infra
    @pytest.mark.parametrize("path", LISTINGS)
    def test_a_listing_contains_only_the_callers_own_records(
        self, client: TestClient, tenants, path: str
    ) -> None:
        attacker, victim = tenants

        response = client.get(path, headers=attacker.headers)

        assert response.status_code == 200, path
        assert [marker for marker in victim.markers if marker in response.text] == []

    @requires_infra
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/execution-plans?organization_id={org}",
            "/v1/recommendations?organization_id={org}&user_id={user}",
            "/v1/archives?organization_id={org}",
            "/v1/connectors?organization_id={org}",
            "/v1/storage/duplicates?organization_id={org}&limit=100",
            "/v1/storage/large-files?min_size_bytes=1&organization_id={org}&limit=100",
            "/v1/approvals?status=pending&organization_id={org}",
            "/v1/execution-jobs?status=pending&organization_id={org}",
            "/v1/workflows?organization_id={org}",
            "/v1/notifications?user_id={user}",
        ],
    )
    def test_smuggled_tenant_parameters_are_ignored(
        self, client: TestClient, tenants, path: str
    ) -> None:
        attacker, victim = tenants

        response = client.get(
            path.format(org=victim.ids["organization_id"], user=victim.ids["user_id"]),
            headers={**attacker.headers, "X-Organization-Id": victim.ids["organization_id"]},
        )

        assert response.status_code == 200
        assert [marker for marker in victim.markers if marker in response.text] == []

    @requires_infra
    def test_a_recommendation_search_filter_cannot_find_another_tenants_titles(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants

        response = client.get(
            "/v1/recommendations",
            params={"search": victim.ids["recommendation_title"]},
            headers=attacker.headers,
        )

        assert response.status_code == 200
        assert response.json()["items"] == []

    @requires_infra
    def test_the_attackers_own_data_is_present_so_the_checks_are_not_vacuous(
        self, client: TestClient, tenants
    ) -> None:
        attacker, _victim = tenants

        for path, marker in (
            ("/v1/recommendations", attacker.ids["recommendation_id"]),
            ("/v1/execution-plans", attacker.ids["execution_plan_id"]),
            ("/v1/archives", attacker.ids["archive_id"]),
            ("/v1/conversations", attacker.ids["conversation_id"]),
            ("/v1/workflows", attacker.ids["workflow_id"]),
            ("/v1/connectors", attacker.ids["connector_id"]),
            ("/v1/storage/duplicates", attacker.ids["duplicate_group_id"]),
        ):
            response = client.get(path, headers=attacker.headers)
            assert marker in response.text, path

    @requires_infra
    def test_storage_totals_are_the_callers_own_not_a_blend(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants

        overview = client.get("/v1/storage/overview", headers=attacker.headers).json()

        assert overview["total_files"] == attacker.total_files
        assert overview["total_files"] != victim.total_files


class TestSearchAndAIDoNotCrossTenants:
    @requires_infra
    def test_search_never_returns_another_tenants_files(self, client: TestClient, tenants) -> None:
        attacker, victim = tenants

        by_name = client.post(
            "/v1/search", headers=attacker.headers, json={"query": victim.ids["file_name"]}
        )
        by_id = client.post(
            "/v1/search", headers=attacker.headers, json={"query": victim.ids["file_id"]}
        )
        by_content = client.post(
            "/v1/search",
            headers=attacker.headers,
            json={"query": "confidential payroll figures and salary bands"},
        )

        # A response may echo the attacker's own query, so a query that
        # names the victim's file is judged by what it *returned*.
        for response in (by_name, by_id, by_content):
            assert response.status_code == 200
            for result in response.json()["results"]:
                assert result["file_id"] not in victim.ids.values()
                assert not result["name"].startswith(victim.label)
                assert not result["path"].startswith(f"/{victim.label}")
        assert [m for m in victim.markers if m in by_content.text] == []
        assert attacker.ids["file_id"] in by_content.text

    @requires_infra
    def test_the_assistant_never_cites_another_tenants_files(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants
        naming_the_victim = [
            f"Find the file {victim.ids['file_name']}",
            f"Show details for file {victim.ids['file_id']}",
            f"Which files are in duplicate group {victim.ids['duplicate_group_id']}?",
        ]
        not_naming_the_victim = [
            "What are the salary bands in the confidential payroll figures?",
            "How many duplicate files do I have and how much storage do they use?",
            "Show my largest files",
        ]

        for question in naming_the_victim + not_naming_the_victim:
            response = client.post(
                "/v1/conversations", headers=attacker.headers, json={"question": question}
            )
            assert response.status_code in (200, 201), question
            answer = response.json()["assistant_message"]
            for citation in answer["citations"]:
                assert citation["file_id"] not in victim.ids.values(), question
                assert not citation["file_name"].startswith(victim.label), question
            if question in not_naming_the_victim:
                assert [m for m in victim.markers if m in response.text] == [], question
            else:
                # The echo of the attacker's own question is not a leak; the
                # assistant's answer must still not reveal the victim's data.
                assert victim.label not in answer["content"].replace(question, ""), question

    @requires_infra
    def test_search_results_belong_to_the_callers_organization_in_the_database(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants

        response = client.post(
            "/v1/search",
            headers=attacker.headers,
            json={"query": "confidential payroll figures and salary bands"},
        )

        returned = {result["file_id"] for result in response.json()["results"]}
        assert victim.ids["file_id"] not in returned
        assert returned <= {attacker.ids["file_id"], attacker.ids["duplicate_file_id"]}


class TestIdentityClaimsCannotWidenAccess:
    @requires_infra
    def test_a_token_claiming_the_victims_organization_still_resolves_to_the_users_own(
        self, client: TestClient, tenants
    ) -> None:
        attacker, victim = tenants
        forged = create_access_token(
            AccessTokenClaims(
                user_id=attacker.user.id, organization_id=victim.organization_id, role="owner"
            )
        )

        response = client.get("/v1/execution-plans", headers={"Authorization": f"Bearer {forged}"})

        assert response.status_code == 200
        assert [m for m in victim.markers if m in response.text] == []
        assert attacker.ids["execution_plan_id"] in response.text

    @requires_infra
    def test_a_token_for_a_nonexistent_user_is_rejected(self, client: TestClient, tenants) -> None:
        attacker, victim = tenants
        forged = create_access_token(
            AccessTokenClaims(
                user_id=uuid.uuid4(), organization_id=victim.organization_id, role="owner"
            )
        )

        response = client.get("/v1/execution-plans", headers={"Authorization": f"Bearer {forged}"})

        assert response.status_code == 401

    @requires_infra
    @pytest.mark.parametrize(
        "path",
        ["/v1/execution-plans", "/v1/files/{file_id}", "/v1/archives", "/v1/storage/overview"],
    )
    def test_requests_without_credentials_are_rejected(
        self, client: TestClient, tenants, path: str
    ) -> None:
        _attacker, victim = tenants

        response = client.get(path.format(**victim.ids))

        assert response.status_code == 401
        assert [m for m in victim.markers if m in response.text] == []

    @requires_infra
    def test_a_role_claim_in_the_token_does_not_grant_privileges(
        self, client: TestClient, tenants
    ) -> None:
        attacker, _victim = tenants
        session = get_session_factory()()
        try:
            member_role_id = UserRepository(session).get_by_id(attacker.user.id).role_id
            assert member_role_id is not None
        finally:
            session.close()
        forged = create_access_token(
            AccessTokenClaims(
                user_id=attacker.user.id, organization_id=attacker.organization_id, role="owner"
            )
        )

        response = client.get("/v1/users/me", headers={"Authorization": f"Bearer {forged}"})

        assert response.status_code == 200
        assert response.json()["role"] == attacker.user.role.name
