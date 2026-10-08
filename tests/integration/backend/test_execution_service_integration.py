import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from app.application.approval_service import ApprovalService
from app.application.auth_service import AuthService
from app.application.execution_job_service import ExecutionJobService
from app.application.execution_plan_service import ExecutionPlanService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import ConflictError, NotFoundError, ValidationError, get_settings
from vault_shared.db.models import (
    ApprovalStatus,
    ConnectorProvider,
    DriveType,
    ExecutionActionType,
    ExecutionJobStatus,
    ExecutionPlanStatus,
    RecommendationStatus,
)
from vault_shared.db.repositories import (
    ApprovalRequestRepository,
    ArchiveJobRepository,
    ConnectorCredentialsRepository,
    DuplicateGroupRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionStepRepository,
    FileRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    RollbackRecordRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
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
        sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        email_verified=True,
        name="Ada Founder",
        picture=None,
    )
    session = AuthService(db).complete_google_login(google_user=google_user, ip_address=None)
    return session.user


def _provision_connector(
    db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID, granted_scopes: str
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
        granted_scopes=granted_scopes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return connector


def _provision_file(
    db: Session, *, connector_id: uuid.UUID, name: str, provider_file_id: str, size_bytes: int = 100
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=provider_file_id,
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=size_bytes,
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
    rule_name: str,
    affected_file_ids: list[uuid.UUID],
    status: str = RecommendationStatus.ACTIVE,
):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id, triggered_by="manual", triggered_by_user_id=None
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=organization_id,
        recommendation_job_id=job.id,
        rule_name=rule_name,
        category="storage_optimization",
        title=f"{rule_name} fired",
        description="A test recommendation.",
        confidence=0.8,
        estimated_impact="some impact",
        impact_value=10.0,
        risk_level="low",
        suggested_action="Do something.",
        related_departments=[],
        affected_file_ids=[str(fid) for fid in affected_file_ids],
        priority_score=50.0,
    )
    if status != RecommendationStatus.ACTIVE:
        recommendation.status = status
        db.flush()
    db.commit()
    return recommendation


def _provision_completed_archive(
    db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID, file_ids: list[uuid.UUID]
):
    """A minimal COMPLETED ArchiveJob backing `file_ids` — the eligibility
    signal `create_permanent_delete_plan` requires (a file can only be
    permanently deleted once it's already backed up)."""
    plan = ExecutionPlanRepository(db).create(
        organization_id=organization_id,
        recommendation_id=None,
        duplicate_group_id=None,
        created_by_user_id=user_id,
        target_provider="google_workspace",
        estimated_impact="test archive",
        estimated_storage_savings_bytes=None,
        risk_level="low",
        rollback_available=True,
        required_permissions=[],
    )
    db.commit()
    archive_job = ArchiveJobRepository(db).create(
        organization_id=organization_id,
        execution_plan_id=plan.id,
        name="Test Archive",
        created_by_user_id=user_id,
    )
    ArchiveJobRepository(db).mark_completed(
        archive_job,
        object_storage_key="archives/test.zip",
        original_size_bytes=100,
        compressed_size_bytes=50,
        file_count=len(file_ids),
        manifest=[
            {
                "file_id": str(fid),
                "name": "test.pdf",
                "path": "/test.pdf",
                "size_bytes": 100,
                "mime_type": "application/pdf",
                "checksum_sha256": "abc123",
            }
            for fid in file_ids
        ],
    )
    db.commit()
    return archive_job


# ----------------------------------------------------------------------
# ExecutionPlanService
# ----------------------------------------------------------------------


@requires_infra
def test_create_plan_builds_a_step_per_affected_file_with_low_risk(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    file_b = _provision_file(db, connector_id=connector.id, name="B.txt", provider_file_id="f-b")
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[file_a.id, file_b.id],
    )
    service = ExecutionPlanService(db)

    plan = service.create_plan(
        recommendation.id, organization_id=user.organization_id, user_id=user.id
    )

    assert plan.status == ExecutionPlanStatus.PENDING_APPROVAL
    assert plan.risk_level == "low"
    assert plan.rollback_available is True

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert len(detail.steps) == 2
    assert {s.action_type for s in detail.steps} == {ExecutionActionType.REMOVE_DUPLICATE}

    approval = ApprovalRequestRepository(db).get_by_plan(plan.id)
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING


@requires_infra
def test_create_plan_rejects_a_non_executable_rule(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="orphaned_ownership",
        affected_file_ids=[file_a.id],
    )
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_plan(
            recommendation.id, organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_plan_rejects_a_resolved_recommendation(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[file_a.id],
        status=RecommendationStatus.RESOLVED,
    )
    service = ExecutionPlanService(db)

    with pytest.raises(ConflictError):
        service.create_plan(
            recommendation.id, organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_plan_rejects_a_second_plan_while_one_is_active(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[file_a.id],
    )
    service = ExecutionPlanService(db)
    service.create_plan(recommendation.id, organization_id=user.organization_id, user_id=user.id)

    with pytest.raises(ConflictError):
        service.create_plan(
            recommendation.id, organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_plan_computes_high_risk_for_many_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    files = [
        _provision_file(db, connector_id=connector.id, name=f"F{i}.txt", provider_file_id=f"f-{i}")
        for i in range(101)
    ]
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="archive_candidates",
        affected_file_ids=[f.id for f in files],
    )
    service = ExecutionPlanService(db)

    plan = service.create_plan(
        recommendation.id, organization_id=user.organization_id, user_id=user.id
    )

    assert plan.risk_level == "high"


def _provision_duplicate_group(
    db: Session, *, organization_id: uuid.UUID, keep_file, other_files: list
):
    analysis_job = StorageAnalysisJobRepository(db).create(
        organization_id=organization_id, triggered_by="manual", triggered_by_user_id=None
    )
    db.commit()
    group = DuplicateGroupRepository(db).upsert_group(
        organization_id=organization_id,
        storage_analysis_job_id=analysis_job.id,
        checksum="test-checksum",
        file_count=1 + len(other_files),
        total_size_bytes=sum(f.size_bytes or 0 for f in [keep_file, *other_files]),
        recoverable_size_bytes=sum(f.size_bytes or 0 for f in other_files),
        recommended_keep_file_id=keep_file.id,
        recommended_keep_reason="Test reason.",
        recommended_keep_confidence=0.86,
    )
    DuplicateGroupRepository(db).replace_members(
        group,
        [(keep_file.id, True)] + [(f.id, False) for f in other_files],
    )
    db.commit()
    return group


@requires_infra
def test_refreshing_duplicate_groups_never_erases_the_action_taken_on_them(db: Session) -> None:
    """Storage analysis rebuilds duplicate groups after every cleanup; the
    cleanup itself is a record of a real change in Drive and must survive."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    keep = _provision_file(db, connector_id=connector.id, name="keep.txt", provider_file_id="k")
    dupe = _provision_file(db, connector_id=connector.id, name="dupe.txt", provider_file_id="d")
    group = _provision_duplicate_group(
        db, organization_id=user.organization_id, keep_file=keep, other_files=[dupe]
    )
    service = ExecutionPlanService(db)
    plan = service.create_plan_from_duplicate_group(
        group.id, organization_id=user.organization_id, user_id=user.id
    )
    db.commit()

    db.delete(group)
    db.commit()
    db.expire_all()

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert detail.plan.duplicate_group_id is None
    assert [s.target_file_id for s in detail.steps] == [dupe.id]


@requires_infra
def test_create_plan_from_duplicate_group_targets_only_non_keep_members(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    keep = _provision_file(
        db, connector_id=connector.id, name="keep.txt", provider_file_id="f-keep"
    )
    dupe_a = _provision_file(
        db, connector_id=connector.id, name="dupe_a.txt", provider_file_id="f-a"
    )
    dupe_b = _provision_file(
        db, connector_id=connector.id, name="dupe_b.txt", provider_file_id="f-b"
    )
    group = _provision_duplicate_group(
        db, organization_id=user.organization_id, keep_file=keep, other_files=[dupe_a, dupe_b]
    )
    service = ExecutionPlanService(db)

    plan = service.create_plan_from_duplicate_group(
        group.id, organization_id=user.organization_id, user_id=user.id
    )

    assert plan.duplicate_group_id == group.id
    assert plan.recommendation_id is None
    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    target_ids = {s.target_file_id for s in detail.steps}
    assert target_ids == {dupe_a.id, dupe_b.id}
    assert keep.id not in target_ids
    assert all(s.action_type == ExecutionActionType.REMOVE_DUPLICATE for s in detail.steps)


@requires_infra
def test_create_plan_from_duplicate_group_rejects_a_second_plan_while_one_is_active(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    keep = _provision_file(
        db, connector_id=connector.id, name="keep.txt", provider_file_id="f-keep"
    )
    dupe = _provision_file(
        db, connector_id=connector.id, name="dupe.txt", provider_file_id="f-dupe"
    )
    group = _provision_duplicate_group(
        db, organization_id=user.organization_id, keep_file=keep, other_files=[dupe]
    )
    service = ExecutionPlanService(db)
    service.create_plan_from_duplicate_group(
        group.id, organization_id=user.organization_id, user_id=user.id
    )

    with pytest.raises(ConflictError):
        service.create_plan_from_duplicate_group(
            group.id, organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_plan_from_duplicate_group_rejects_another_organizations_group(
    db: Session,
) -> None:
    user_a = _provision_user(db)
    connector_a = _provision_connector(
        db,
        organization_id=user_a.organization_id,
        user_id=user_a.id,
        granted_scopes=DRIVE_WRITE_SCOPE,
    )
    keep = _provision_file(
        db, connector_id=connector_a.id, name="keep.txt", provider_file_id="f-keep"
    )
    dupe = _provision_file(
        db, connector_id=connector_a.id, name="dupe.txt", provider_file_id="f-dupe"
    )
    group = _provision_duplicate_group(
        db, organization_id=user_a.organization_id, keep_file=keep, other_files=[dupe]
    )

    user_b = _provision_user(db)
    service = ExecutionPlanService(db)

    with pytest.raises(NotFoundError):
        service.create_plan_from_duplicate_group(
            group.id, organization_id=user_b.organization_id, user_id=user_b.id
        )


@requires_infra
def test_create_ad_hoc_plan_targets_exactly_the_selected_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(
        db, connector_id=connector.id, name="old_a.pdf", provider_file_id="f-a"
    )
    file_b = _provision_file(
        db, connector_id=connector.id, name="old_b.pdf", provider_file_id="f-b"
    )
    service = ExecutionPlanService(db)

    plan = service.create_ad_hoc_plan(
        [file_a.id, file_b.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    assert plan.recommendation_id is None
    assert plan.duplicate_group_id is None
    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert {s.target_file_id for s in detail.steps} == {file_a.id, file_b.id}
    assert all(s.action_type == ExecutionActionType.ARCHIVE for s in detail.steps)


@requires_infra
def test_create_ad_hoc_plan_accepts_create_archive(db: Session) -> None:
    """The Archive MVP's "Create Archive" bulk action reuses this exact
    entry point with action_type="create_archive" — this is the only
    change required upstream of ExecutionService to unblock it
    (_AD_HOC_ALLOWED_ACTIONS)."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")

    service = ExecutionPlanService(db)
    plan = service.create_ad_hoc_plan(
        [file_a.id],
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert len(detail.steps) == 1
    assert detail.steps[0].action_type == ExecutionActionType.CREATE_ARCHIVE
    assert detail.steps[0].target_file_id == file_a.id


@requires_infra
def test_create_ad_hoc_plan_rejects_an_unsupported_action(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_ad_hoc_plan(
            [file_a.id],
            action_type=ExecutionActionType.UPDATE_METADATA,
            organization_id=user.organization_id,
            user_id=user.id,
        )


@requires_infra
def test_create_ad_hoc_plan_rename_targets_one_file_with_the_new_name(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="old.pdf", provider_file_id="f-a")
    service = ExecutionPlanService(db)

    plan = service.create_ad_hoc_plan(
        [file_a.id],
        action_type=ExecutionActionType.RENAME,
        organization_id=user.organization_id,
        user_id=user.id,
        new_name="new.pdf",
    )

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert len(detail.steps) == 1
    assert detail.steps[0].target_file_id == file_a.id
    assert detail.steps[0].planned_change["new_name"] == "new.pdf"


@requires_infra
def test_create_ad_hoc_plan_rename_rejects_more_than_one_file(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")
    file_b = _provision_file(db, connector_id=connector.id, name="b.pdf", provider_file_id="f-b")
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_ad_hoc_plan(
            [file_a.id, file_b.id],
            action_type=ExecutionActionType.RENAME,
            organization_id=user.organization_id,
            user_id=user.id,
            new_name="new.pdf",
        )


@requires_infra
def test_create_ad_hoc_plan_rename_rejects_a_missing_new_name(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_ad_hoc_plan(
            [file_a.id],
            action_type=ExecutionActionType.RENAME,
            organization_id=user.organization_id,
            user_id=user.id,
        )


@requires_infra
def test_create_ad_hoc_plan_move_targets_every_selected_file_at_the_new_parent(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")
    file_b = _provision_file(db, connector_id=connector.id, name="b.pdf", provider_file_id="f-b")
    service = ExecutionPlanService(db)

    plan = service.create_ad_hoc_plan(
        [file_a.id, file_b.id],
        action_type=ExecutionActionType.MOVE_FILE,
        organization_id=user.organization_id,
        user_id=user.id,
        new_parent_id="folder-target",
    )

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert len(detail.steps) == 2
    assert all(s.planned_change["new_parent_id"] == "folder-target" for s in detail.steps)


@requires_infra
def test_create_ad_hoc_plan_move_rejects_a_missing_destination(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="a.pdf", provider_file_id="f-a")
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_ad_hoc_plan(
            [file_a.id],
            action_type=ExecutionActionType.MOVE_FILE,
            organization_id=user.organization_id,
            user_id=user.id,
        )


@requires_infra
def test_create_ad_hoc_plan_silently_drops_a_file_id_from_another_organization(
    db: Session,
) -> None:
    """Security-critical: a caller-supplied file id list is not already
    org-scoped the way a Recommendation's or DuplicateGroup's affected
    files are — `create_ad_hoc_plan` must re-verify ownership itself,
    never trust the request."""
    user_a = _provision_user(db)
    connector_a = _provision_connector(
        db,
        organization_id=user_a.organization_id,
        user_id=user_a.id,
        granted_scopes=DRIVE_WRITE_SCOPE,
    )
    file_a = _provision_file(db, connector_id=connector_a.id, name="a.pdf", provider_file_id="f-a")

    user_b = _provision_user(db)
    connector_b = _provision_connector(
        db,
        organization_id=user_b.organization_id,
        user_id=user_b.id,
        granted_scopes=DRIVE_WRITE_SCOPE,
    )
    file_b = _provision_file(db, connector_id=connector_b.id, name="b.pdf", provider_file_id="f-b")

    service = ExecutionPlanService(db)
    plan = service.create_ad_hoc_plan(
        [file_a.id, file_b.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=user_a.organization_id,
        user_id=user_a.id,
    )

    detail = service.get_detail(plan.id, organization_id=user_a.organization_id)
    target_ids = {s.target_file_id for s in detail.steps}
    assert target_ids == {file_a.id}
    assert file_b.id not in target_ids


@requires_infra
def test_create_ad_hoc_plan_rejects_an_empty_selection(db: Session) -> None:
    user = _provision_user(db)
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_ad_hoc_plan(
            [],
            action_type=ExecutionActionType.ARCHIVE,
            organization_id=user.organization_id,
            user_id=user.id,
        )


@requires_infra
def test_create_permanent_delete_plan_targets_only_trashed_and_archived_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    files = FileRepository(db)
    trashed = _provision_file(
        db, connector_id=connector.id, name="trashed.pdf", provider_file_id="f-a"
    )
    files.mark_trashed(trashed, trashed=True)
    _provision_completed_archive(
        db, organization_id=user.organization_id, user_id=user.id, file_ids=[trashed.id]
    )
    active = _provision_file(
        db, connector_id=connector.id, name="active.pdf", provider_file_id="f-b"
    )
    service = ExecutionPlanService(db)

    plan = service.create_permanent_delete_plan(
        [trashed.id, active.id], organization_id=user.organization_id, user_id=user.id
    )

    detail = service.get_detail(plan.id, organization_id=user.organization_id)
    assert {s.target_file_id for s in detail.steps} == {trashed.id}
    assert all(s.action_type == ExecutionActionType.PERMANENT_DELETE for s in detail.steps)
    assert plan.rollback_available is False


@requires_infra
def test_create_permanent_delete_plan_rejects_when_nothing_is_eligible(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    active = _provision_file(
        db, connector_id=connector.id, name="active.pdf", provider_file_id="f-a"
    )
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_permanent_delete_plan(
            [active.id], organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_permanent_delete_plan_rejects_a_trashed_file_with_no_archive_backup(
    db: Session,
) -> None:
    """The safety net this session added on top of "already trashed":
    a file must also already be backed up by a completed archive before
    it's eligible for real, unrecoverable deletion — trashed alone isn't
    enough."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    trashed = _provision_file(
        db, connector_id=connector.id, name="trashed.pdf", provider_file_id="f-a"
    )
    FileRepository(db).mark_trashed(trashed, trashed=True)
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_permanent_delete_plan(
            [trashed.id], organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_permanent_delete_plan_rejects_a_file_whose_backing_archive_was_deleted(
    db: Session,
) -> None:
    """Deleting the backup zip revokes the eligibility it granted — a
    file's archive being `COMPLETED` in the past isn't enough, it has to
    still be `COMPLETED` now."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    trashed = _provision_file(
        db, connector_id=connector.id, name="trashed.pdf", provider_file_id="f-a"
    )
    FileRepository(db).mark_trashed(trashed, trashed=True)
    archive_job = _provision_completed_archive(
        db, organization_id=user.organization_id, user_id=user.id, file_ids=[trashed.id]
    )
    ArchiveJobRepository(db).mark_deleted(archive_job)
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_permanent_delete_plan(
            [trashed.id], organization_id=user.organization_id, user_id=user.id
        )


@requires_infra
def test_create_permanent_delete_plan_excludes_already_permanently_deleted_files(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    files = FileRepository(db)
    gone = _provision_file(db, connector_id=connector.id, name="gone.pdf", provider_file_id="f-a")
    files.mark_trashed(gone, trashed=True)
    files.mark_permanently_deleted(gone)
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_permanent_delete_plan(
            [gone.id], organization_id=user.organization_id, user_id=user.id
        )


# ----------------------------------------------------------------------
# ApprovalService
# ----------------------------------------------------------------------


def _provision_plan_with_approval(
    db: Session, *, user, granted_scopes: str, rule_name: str = "duplicate_files"
):
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=granted_scopes
    )
    file_a = _provision_file(
        db, connector_id=connector.id, name="A.txt", provider_file_id=f"f-{uuid.uuid4().hex[:8]}"
    )
    recommendation = _provision_recommendation(
        db, organization_id=user.organization_id, rule_name=rule_name, affected_file_ids=[file_a.id]
    )
    plan = ExecutionPlanService(db).create_plan(
        recommendation.id, organization_id=user.organization_id, user_id=user.id
    )
    approval = ApprovalRequestRepository(db).get_by_plan(plan.id)
    return plan, approval


@requires_infra
def test_decide_approve_is_blocked_when_the_connector_lacks_write_scope(db: Session) -> None:
    user = _provision_user(db)
    _plan, approval = _provision_plan_with_approval(
        db, user=user, granted_scopes="https://www.googleapis.com/auth/drive.readonly"
    )
    service = ApprovalService(db)

    with pytest.raises(ValidationError):
        service.decide(
            approval.id,
            organization_id=user.organization_id,
            user_id=user.id,
            decision="approve",
            comments=None,
            ip_address=None,
        )


@requires_infra
def test_decide_approve_creates_an_execution_job_when_permissions_are_satisfied(
    db: Session,
) -> None:
    user = _provision_user(db)
    plan, approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    service = ApprovalService(db)

    decided = service.decide(
        approval.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="approve",
        comments="go ahead",
        ip_address="127.0.0.1",
    )

    assert decided.status == ApprovalStatus.APPROVED
    updated_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert updated_plan.status == ExecutionPlanStatus.APPROVED

    jobs = ExecutionJobRepository(db).list_for_plan(plan.id)
    assert len(jobs) == 1
    assert jobs[0].status == ExecutionJobStatus.PENDING
    assert jobs[0].triggered_by_user_id == user.id


@requires_infra
def test_require_approval_false_approves_and_starts_the_job_with_no_approval_request(
    db: Session,
) -> None:
    """ADR-026's replacement for the old auto-approval hop: a direct plan
    is approved and its job started in the one `create_plan` call, with no
    `ApprovalRequest` ever created."""
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(
        db, connector_id=connector.id, name="A.txt", provider_file_id=f"f-{uuid.uuid4().hex[:8]}"
    )
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[file_a.id],
    )

    plan = ExecutionPlanService(db).create_plan(
        recommendation.id,
        organization_id=user.organization_id,
        user_id=user.id,
        require_approval=False,
    )

    assert plan.status == ExecutionPlanStatus.APPROVED
    assert ApprovalRequestRepository(db).get_by_plan(plan.id) is None
    jobs = ExecutionJobRepository(db).list_for_plan(plan.id)
    assert len(jobs) == 1
    assert jobs[0].triggered_by_user_id == user.id


@requires_infra
def test_require_approval_false_fails_loudly_when_permissions_are_not_satisfied(
    db: Session,
) -> None:
    """The connector lacks write scope — this must fail the request
    outright (ADR-026), not leave a plan silently waiting for a review
    nobody performs anymore."""
    user = _provision_user(db)
    connector = _provision_connector(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        granted_scopes="https://www.googleapis.com/auth/drive.readonly",
    )
    file_a = _provision_file(
        db, connector_id=connector.id, name="A.txt", provider_file_id=f"f-{uuid.uuid4().hex[:8]}"
    )
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        affected_file_ids=[file_a.id],
    )
    service = ExecutionPlanService(db)

    with pytest.raises(ValidationError):
        service.create_plan(
            recommendation.id,
            organization_id=user.organization_id,
            user_id=user.id,
            require_approval=False,
        )

    # Nothing was left half-created: the plan was flushed but never
    # committed, so the session rolling back on close discards it entirely
    # — no dangling plan for a mutation that was never going to run.
    db.rollback()
    assert ExecutionPlanRepository(db).list_for_organization(user.organization_id) == []


@requires_infra
def test_decide_reject_is_terminal_and_creates_no_job(db: Session) -> None:
    user = _provision_user(db)
    plan, approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    service = ApprovalService(db)

    decided = service.decide(
        approval.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="reject",
        comments=None,
        ip_address=None,
    )

    assert decided.status == ApprovalStatus.REJECTED
    updated_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert updated_plan.status == ExecutionPlanStatus.REJECTED
    assert ExecutionJobRepository(db).list_for_plan(plan.id) == []

    with pytest.raises(ConflictError):
        service.decide(
            approval.id,
            organization_id=user.organization_id,
            user_id=user.id,
            decision="approve",
            comments=None,
            ip_address=None,
        )


@requires_infra
def test_bulk_decide_isolates_a_bad_id_from_the_rest(db: Session) -> None:
    user = _provision_user(db)
    _plan1, approval1 = _provision_plan_with_approval(
        db, user=user, granted_scopes=DRIVE_WRITE_SCOPE
    )
    service = ApprovalService(db)

    results = service.bulk_decide(
        [approval1.id, uuid.uuid4()],
        organization_id=user.organization_id,
        user_id=user.id,
        decision="reject",
        comments=None,
        ip_address=None,
    )

    assert len(results) == 1
    assert results[0].id == approval1.id
    assert results[0].status == ApprovalStatus.REJECTED


# ----------------------------------------------------------------------
# ExecutionJobService
# ----------------------------------------------------------------------


@requires_infra
def test_cancel_rejects_an_already_completed_job(db: Session) -> None:
    user = _provision_user(db)
    plan, approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    ApprovalService(db).decide(
        approval.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="approve",
        comments=None,
        ip_address=None,
    )
    jobs = ExecutionJobRepository(db)
    job = jobs.list_for_plan(plan.id)[0]
    jobs.mark_completed(job)
    db.commit()

    service = ExecutionJobService(db)
    with pytest.raises(ConflictError):
        service.cancel(job, organization_id=user.organization_id, user_id=user.id)


@requires_infra
def test_pause_only_allowed_while_running(db: Session) -> None:
    user = _provision_user(db)
    plan, approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    ApprovalService(db).decide(
        approval.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="approve",
        comments=None,
        ip_address=None,
    )
    jobs = ExecutionJobRepository(db)
    job = jobs.list_for_plan(plan.id)[0]

    service = ExecutionJobService(db)
    with pytest.raises(ConflictError):
        service.pause(job, organization_id=user.organization_id, user_id=user.id)

    jobs.mark_running(job)
    db.commit()
    paused = service.pause(job, organization_id=user.organization_id, user_id=user.id)
    assert jobs.is_pause_requested(paused.id) is True


@requires_infra
def test_resume_only_allowed_while_paused(db: Session) -> None:
    user = _provision_user(db)
    plan, approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    ApprovalService(db).decide(
        approval.id,
        organization_id=user.organization_id,
        user_id=user.id,
        decision="approve",
        comments=None,
        ip_address=None,
    )
    jobs = ExecutionJobRepository(db)
    job = jobs.list_for_plan(plan.id)[0]
    jobs.mark_running(job)
    jobs.mark_paused(job)
    db.commit()

    service = ExecutionJobService(db)
    resumed = service.resume(job, organization_id=user.organization_id, user_id=user.id)
    assert resumed.status == ExecutionJobStatus.PENDING


@requires_infra
def test_trigger_rollback_requires_a_rollbackable_record(db: Session) -> None:
    user = _provision_user(db)
    plan, _approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    service = ExecutionJobService(db)

    with pytest.raises(ConflictError):
        service.trigger_rollback(plan.id, organization_id=user.organization_id, user_id=user.id)


@requires_infra
def test_trigger_rollback_creates_a_rollback_job_when_a_record_exists(db: Session) -> None:
    user = _provision_user(db)
    plan, _approval = _provision_plan_with_approval(db, user=user, granted_scopes=DRIVE_WRITE_SCOPE)
    step = ExecutionStepRepository(db).list_for_plan(plan.id)[0]
    RollbackRecordRepository(db).create(execution_step_id=step.id, pre_state={"trashed": False})
    db.commit()

    service = ExecutionJobService(db)
    job = service.trigger_rollback(plan.id, organization_id=user.organization_id, user_id=user.id)

    assert job.is_rollback is True
    assert job.status == ExecutionJobStatus.PENDING

    with pytest.raises(ConflictError):
        service.trigger_rollback(plan.id, organization_id=user.organization_id, user_id=user.id)


@requires_infra
def test_trigger_rollback_rejects_an_unknown_plan(db: Session) -> None:
    user = _provision_user(db)
    service = ExecutionJobService(db)

    with pytest.raises(NotFoundError):
        service.trigger_rollback(
            uuid.uuid4(), organization_id=user.organization_id, user_id=user.id
        )
