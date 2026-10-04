import socket
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from vault_shared import NotFoundError, get_settings
from vault_shared.connectors.google_drive import DriveFile
from vault_shared.connectors.google_workspace import GoogleAccountInfo, GoogleTokenSet
from vault_shared.db.models import (
    ArchiveJobStatus,
    ConnectorProvider,
    DriveType,
    ExecutionActionType,
    ExecutionJobStatus,
    ExecutionPlanStatus,
    ExecutionResultStatus,
    ExecutionStepStatus,
    RecommendationTrigger,
    RoleName,
    StorageAnalysisTrigger,
    VerificationStatus,
)
from vault_shared.db.repositories import (
    ArchiveJobRepository,
    ConnectorCredentialsRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
    OrganizationRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    RoleRepository,
    RollbackRecordRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
from vault_shared.security.encryption import encrypt_token
from vault_shared.storage.default_registry import build_storage_registry
from worker.execution.execution_service import ExecutionService


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


class _FakeGoogleWorkspaceOAuthClient:
    def exchange_code(self, *, code: str) -> GoogleTokenSet:
        raise NotImplementedError

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        return GoogleTokenSet(
            access_token="access-refreshed",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes=DRIVE_WRITE_SCOPE,
        )

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        return GoogleAccountInfo(email="founder@acme.com", workspace_domain="acme.com")

    def build_authorize_url(self, *, state: str) -> str:
        raise NotImplementedError

    def revoke(self, *, token: str) -> bool:
        return True


class _FakeGoogleDriveClient:
    """Never calls the real Google Drive API — an in-memory dict of
    `DriveFile`s keyed by provider file id, mutated the same way Drive's
    own API would (trash flag flips, parents change, name changes). This
    is the ONLY way execution-engine tests are allowed to exercise a real
    mutation in this codebase — the founder's explicit "fakes/mocks only"
    constraint for Phase 8 (see ADR-020)."""

    def __init__(self, *, files: dict[str, DriveFile] | None = None) -> None:
        self._files = dict(files or {})
        self.calls: list[tuple[str, str]] = []

    def download_file(self, *, access_token: str, file_id: str) -> bytes:
        self.calls.append(("download_file", file_id))
        return f"contents of {file_id}".encode()

    def export_file(self, *, access_token: str, file_id: str, export_mime_type: str) -> bytes:
        self.calls.append(("export_file", file_id))
        return f"exported {file_id} as {export_mime_type}".encode()

    def stream_file(self, *, access_token: str, file_id: str):
        return iter([self.download_file(access_token=access_token, file_id=file_id)])

    def stream_export(self, *, access_token: str, file_id: str, export_mime_type: str):
        return iter(
            [
                self.export_file(
                    access_token=access_token, file_id=file_id, export_mime_type=export_mime_type
                )
            ]
        )

    def list_shared_drives(self, *, access_token: str) -> list:
        return []

    def get_file(self, *, access_token: str, file_id: str) -> DriveFile:
        self.calls.append(("get_file", file_id))
        if file_id not in self._files:
            raise NotFoundError(f"File {file_id} not found.")
        return self._files[file_id]

    def move_file(
        self, *, access_token: str, file_id: str, add_parent_id: str, remove_parent_id: str
    ) -> DriveFile:
        self.calls.append(("move_file", file_id))
        current = self._files[file_id]
        parents = [p for p in current.parents if p != remove_parent_id]
        parents.append(add_parent_id)
        updated = replace(current, parents=parents)
        self._files[file_id] = updated
        return updated

    def rename_file(self, *, access_token: str, file_id: str, new_name: str) -> DriveFile:
        self.calls.append(("rename_file", file_id))
        updated = replace(self._files[file_id], name=new_name)
        self._files[file_id] = updated
        return updated

    def set_trashed(self, *, access_token: str, file_id: str, trashed: bool) -> DriveFile:
        self.calls.append(("set_trashed", file_id))
        updated = replace(self._files[file_id], trashed=trashed)
        self._files[file_id] = updated
        return updated

    def update_app_properties(
        self, *, access_token: str, file_id: str, properties: dict[str, str]
    ) -> DriveFile:
        self.calls.append(("update_app_properties", file_id))
        return self._files[file_id]

    def delete_file(self, *, access_token: str, file_id: str) -> None:
        self.calls.append(("delete_file", file_id))
        self._files.pop(file_id, None)


class _FakeObjectStorageClient:
    """In-memory stand-in — the Phase 8 fakes/mocks-only rule (ADR-020)
    extends naturally to the Archive MVP's object store: no test may hit a
    real MinIO/S3 bucket."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []

    def put_object(self, *, key: str, body, content_type: str) -> None:
        self.put_calls.append(key)
        self._objects[key] = body.read()
        # Mirrors boto3's upload_fileobj, which closes the passed file
        # object once the transfer completes — a real bug (reading
        # compressed_size via buffer.tell() *after* this call) shipped
        # once because this fake didn't replicate that and silently let
        # every test pass against a client that behaves differently from
        # the real one.
        body.close()

    def get_object_stream(self, *, key: str):
        yield self._objects[key]

    def delete_object(self, *, key: str) -> None:
        self.delete_calls.append(key)
        self._objects.pop(key, None)


def _drive_file(
    *, file_id: str, name: str = "Doc.txt", parents: list[str] | None = None, trashed: bool = False
) -> DriveFile:
    now = datetime.now(UTC)
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="text/plain",
        parents=parents or ["root"],
        size=100,
        created_time=now,
        modified_time=now,
        viewed_by_me_time=None,
        owner_email="founder@acme.com",
        shared=False,
        checksum=None,
        version_id=None,
        is_folder=False,
        trashed=trashed,
    )


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


def _provision_file(db: Session, *, connector_id: uuid.UUID, name: str, provider_file_id: str):
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


def _provision_plan(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id, triggered_by="manual", triggered_by_user_id=None
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=organization_id,
        recommendation_job_id=job.id,
        rule_name="duplicate_files",
        category="storage_optimization",
        title="Duplicates found",
        description="A test recommendation.",
        confidence=0.9,
        estimated_impact="some impact",
        impact_value=10.0,
        risk_level="low",
        suggested_action="Remove them.",
        related_departments=[],
        affected_file_ids=[],
        priority_score=50.0,
    )
    plan = ExecutionPlanRepository(db).create(
        organization_id=organization_id,
        recommendation_id=recommendation.id,
        created_by_user_id=user_id,
        target_provider="google_workspace",
        estimated_impact="1 file",
        estimated_storage_savings_bytes=100,
        risk_level="low",
        rollback_available=True,
        required_permissions=[DRIVE_WRITE_SCOPE],
    )
    db.commit()
    return plan


def _provision_ad_hoc_plan(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    """Unlike `_provision_plan`, creates no `RecommendationJob` — that
    fixture's own job is left PENDING forever (never marked complete),
    which would make `RecommendationJobRepository.has_active_job` see an
    "active" job for the org and correctly (by design) skip creating a
    new one. Used by tests asserting on `_trigger_storage_refresh`'s own
    job-creation behavior, where that dangling fixture job would be a
    false blocker rather than anything real."""
    plan = ExecutionPlanRepository(db).create(
        organization_id=organization_id,
        recommendation_id=None,
        duplicate_group_id=None,
        created_by_user_id=user_id,
        target_provider="google_workspace",
        estimated_impact="1 file",
        estimated_storage_savings_bytes=100,
        risk_level="low",
        rollback_available=True,
        required_permissions=[DRIVE_WRITE_SCOPE],
    )
    db.commit()
    return plan


def _provision_step(
    db: Session, *, plan_id: uuid.UUID, file_id: uuid.UUID, action_type: str, order: int = 0
):
    step = ExecutionStepRepository(db).create(
        execution_plan_id=plan_id,
        step_order=order,
        action_type=action_type,
        target_file_id=file_id,
        pre_state={},
        planned_change={"action": action_type},
    )
    db.commit()
    return step


def _provision_job(
    db: Session,
    *,
    plan_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    is_rollback: bool = False,
):
    job = ExecutionJobRepository(db).create(
        execution_plan_id=plan_id,
        organization_id=organization_id,
        triggered_by_user_id=user_id,
        is_rollback=is_rollback,
    )
    db.commit()
    return job


@requires_infra
def test_forward_execution_trashes_the_file_and_completes_job_and_plan(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    step = _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    completed_job = ExecutionJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ExecutionJobStatus.COMPLETED

    completed_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert completed_plan.status == ExecutionPlanStatus.COMPLETED

    completed_step = ExecutionStepRepository(db).get_by_id(step.id)
    assert completed_step.status == ExecutionStepStatus.COMPLETED

    result = ExecutionResultRepository(db).get_by_job_and_step(
        execution_job_id=job.id, execution_step_id=step.id
    )
    assert result.status == ExecutionResultStatus.SUCCESS
    assert result.verification_status == VerificationStatus.VERIFIED

    rollback_record = RollbackRecordRepository(db).get_by_step(step.id)
    assert rollback_record is not None
    assert rollback_record.pre_state == {"trashed": False}
    assert rollback_record.rolled_back is False

    assert drive._files["f-dup"].trashed is True
    assert ("set_trashed", "f-dup") in drive.calls


@requires_infra
def test_rollback_restores_the_file_and_marks_plan_rolled_back(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    step = _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    forward_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id
    )

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )
    service.run(forward_job.id)
    assert drive._files["f-dup"].trashed is True

    rollback_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id, is_rollback=True
    )
    service.run(rollback_job.id)

    assert drive._files["f-dup"].trashed is False

    completed_rollback_job = ExecutionJobRepository(db).get_by_id(rollback_job.id)
    assert completed_rollback_job.status == ExecutionJobStatus.COMPLETED

    rolled_back_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert rolled_back_plan.status == ExecutionPlanStatus.ROLLED_BACK

    rolled_back_step = ExecutionStepRepository(db).get_by_id(step.id)
    assert rolled_back_step.status == ExecutionStepStatus.ROLLED_BACK

    rollback_record = RollbackRecordRepository(db).get_by_step(step.id)
    assert rollback_record.rolled_back is True
    assert rollback_record.rolled_back_by_user_id == user.id


@requires_infra
def test_forward_execution_permanently_deletes_an_already_trashed_file(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Old.txt", provider_file_id="f-old")
    FileRepository(db).mark_trashed(file, trashed=True)
    plan = ExecutionPlanRepository(db).create(
        organization_id=user.organization_id,
        recommendation_id=None,
        duplicate_group_id=None,
        created_by_user_id=user.id,
        target_provider="google_workspace",
        estimated_impact="1 file",
        estimated_storage_savings_bytes=100,
        risk_level="low",
        rollback_available=False,
        required_permissions=[DRIVE_WRITE_SCOPE],
    )
    db.commit()
    step = _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.PERMANENT_DELETE
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    drive = _FakeGoogleDriveClient(files={"f-old": _drive_file(file_id="f-old", trashed=True)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    completed_job = ExecutionJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ExecutionJobStatus.COMPLETED

    completed_step = ExecutionStepRepository(db).get_by_id(step.id)
    assert completed_step.status == ExecutionStepStatus.COMPLETED

    result = ExecutionResultRepository(db).get_by_job_and_step(
        execution_job_id=job.id, execution_step_id=step.id
    )
    assert result.status == ExecutionResultStatus.SUCCESS
    assert result.verification_status == VerificationStatus.VERIFIED

    assert "f-old" not in drive._files
    assert ("delete_file", "f-old") in drive.calls

    deleted_file = FileRepository(db).get_by_id(file.id)
    assert deleted_file.permanently_deleted_at is not None


@requires_infra
def test_rollback_of_a_permanent_delete_step_fails_without_crashing(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Old.txt", provider_file_id="f-old")
    FileRepository(db).mark_trashed(file, trashed=True)
    plan = ExecutionPlanRepository(db).create(
        organization_id=user.organization_id,
        recommendation_id=None,
        duplicate_group_id=None,
        created_by_user_id=user.id,
        target_provider="google_workspace",
        estimated_impact="1 file",
        estimated_storage_savings_bytes=100,
        risk_level="low",
        rollback_available=False,
        required_permissions=[DRIVE_WRITE_SCOPE],
    )
    db.commit()
    _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.PERMANENT_DELETE
    )
    forward_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id
    )

    drive = _FakeGoogleDriveClient(files={"f-old": _drive_file(file_id="f-old", trashed=True)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )
    service.run(forward_job.id)

    # The backend router never lets this happen (rollback_available=False
    # blocks the request before a job exists) — this exercises the
    # engine's own defense-in-depth directly.
    rollback_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id, is_rollback=True
    )
    service.run(rollback_job.id)

    failed_rollback_job = ExecutionJobRepository(db).get_by_id(rollback_job.id)
    assert failed_rollback_job.status == ExecutionJobStatus.FAILED


@requires_infra
def test_a_single_steps_failure_does_not_stop_the_job(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    good_file = _provision_file(
        db, connector_id=connector.id, name="Good.txt", provider_file_id="f-good"
    )
    missing_file = _provision_file(
        db, connector_id=connector.id, name="Missing.txt", provider_file_id="f-missing"
    )
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    good_step = _provision_step(
        db, plan_id=plan.id, file_id=good_file.id, action_type=ExecutionActionType.ARCHIVE, order=0
    )
    missing_step = _provision_step(
        db,
        plan_id=plan.id,
        file_id=missing_file.id,
        action_type=ExecutionActionType.ARCHIVE,
        order=1,
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    # "f-missing" is deliberately absent from the fake — simulates a file
    # that no longer exists on Drive (deleted outside this platform since
    # the plan was created), which is exactly what the live "resource
    # existence" check right before mutation is supposed to catch.
    drive = _FakeGoogleDriveClient(files={"f-good": _drive_file(file_id="f-good", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    completed_job = ExecutionJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ExecutionJobStatus.PARTIALLY_COMPLETED

    completed_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert completed_plan.status == ExecutionPlanStatus.PARTIALLY_COMPLETED

    assert (
        ExecutionStepRepository(db).get_by_id(good_step.id).status == ExecutionStepStatus.COMPLETED
    )
    assert (
        ExecutionStepRepository(db).get_by_id(missing_step.id).status == ExecutionStepStatus.FAILED
    )

    failed_result = ExecutionResultRepository(db).get_by_job_and_step(
        execution_job_id=job.id, execution_step_id=missing_step.id
    )
    assert failed_result.status == ExecutionResultStatus.FAILED
    assert drive._files["f-good"].trashed is True


@requires_infra
def test_execution_is_blocked_and_no_drive_call_is_made_when_the_connector_lacks_write_scope(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        granted_scopes="https://www.googleapis.com/auth/drive.readonly",
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    failed_job = ExecutionJobRepository(db).get_by_id(job.id)
    assert failed_job.status == ExecutionJobStatus.FAILED
    assert failed_job.error is not None and "write" in failed_job.error.lower()

    # The whole point of the permission gate: it runs before any Drive
    # call is even attempted, structurally guaranteeing an under-scoped
    # connector can never reach a mutating (or any) Drive call.
    assert drive.calls == []
    assert drive._files["f-dup"].trashed is False


@requires_infra
def test_cancellation_stops_forward_execution_cleanly(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    jobs = ExecutionJobRepository(db)
    jobs.request_cancel(job)
    db.commit()

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    cancelled_job = jobs.get_by_id(job.id)
    assert cancelled_job.status == ExecutionJobStatus.CANCELLED
    assert drive._files["f-dup"].trashed is False


@requires_infra
def test_forward_trash_marks_the_local_file_trashed_and_triggers_a_storage_refresh(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_ad_hoc_plan(db, organization_id=user.organization_id, user_id=user.id)
    _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )

    service.run(job.id)

    trashed_file = FileRepository(db).get_by_id(file.id)
    assert trashed_file.trashed is True

    storage_jobs = StorageAnalysisJobRepository(db).list_for_organization(user.organization_id)
    assert any(j.triggered_by == StorageAnalysisTrigger.EXECUTION_COMPLETED for j in storage_jobs)
    recommendation_jobs = RecommendationJobRepository(db).list_for_organization(
        user.organization_id
    )
    assert any(
        j.triggered_by == RecommendationTrigger.EXECUTION_COMPLETED for j in recommendation_jobs
    )


@requires_infra
def test_rollback_of_a_trash_marks_the_local_file_active_again(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="Dup.txt", provider_file_id="f-dup")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.REMOVE_DUPLICATE
    )
    forward_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id
    )

    drive = _FakeGoogleDriveClient(files={"f-dup": _drive_file(file_id="f-dup", trashed=False)})
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )
    service.run(forward_job.id)
    assert FileRepository(db).get_by_id(file.id).trashed is True

    rollback_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id, is_rollback=True
    )
    service.run(rollback_job.id)

    assert FileRepository(db).get_by_id(file.id).trashed is False


@requires_infra
def test_create_archive_batch_completes_all_steps_and_produces_one_archive_job(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file_a = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    file_b = _provision_file(db, connector_id=connector.id, name="B.txt", provider_file_id="f-b")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    step_a = _provision_step(
        db,
        plan_id=plan.id,
        file_id=file_a.id,
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        order=0,
    )
    step_b = _provision_step(
        db,
        plan_id=plan.id,
        file_id=file_b.id,
        action_type=ExecutionActionType.CREATE_ARCHIVE,
        order=1,
    )
    job = _provision_job(db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id)

    # CREATE_ARCHIVE only ever reads Drive (download_file) — get_file/
    # set_trashed etc. are never called for this action type, unlike every
    # other one, so neither file needs a fake DriveFile registered.
    drive = _FakeGoogleDriveClient()
    storage = _FakeObjectStorageClient()
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=storage,
    )

    service.run(job.id)

    completed_job = ExecutionJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ExecutionJobStatus.COMPLETED

    completed_plan = ExecutionPlanRepository(db).get_by_id(plan.id)
    assert completed_plan.status == ExecutionPlanStatus.COMPLETED

    assert ExecutionStepRepository(db).get_by_id(step_a.id).status == ExecutionStepStatus.COMPLETED
    assert ExecutionStepRepository(db).get_by_id(step_b.id).status == ExecutionStepStatus.COMPLETED

    # One batch write, not one per file — the whole point of this action
    # type's special-cased handling.
    assert len(storage.put_calls) == 1
    assert ("download_file", "f-a") in drive.calls
    assert ("download_file", "f-b") in drive.calls
    # CREATE_ARCHIVE never mutates Drive — no get_file/set_trashed/etc call.
    assert not any(call[0] != "download_file" for call in drive.calls)

    archive_job = ArchiveJobRepository(db).get_by_plan_id(plan.id)
    assert archive_job is not None
    assert archive_job.status == ArchiveJobStatus.COMPLETED
    assert archive_job.file_count == 2
    assert {entry["file_id"] for entry in archive_job.manifest} == {str(file_a.id), str(file_b.id)}
    assert archive_job.object_storage_key in storage.put_calls

    for step_id in (step_a.id, step_b.id):
        result = ExecutionResultRepository(db).get_by_job_and_step(
            execution_job_id=job.id, execution_step_id=step_id
        )
        assert result.status == ExecutionResultStatus.SUCCESS
        rollback_record = RollbackRecordRepository(db).get_by_step(step_id)
        assert rollback_record.pre_state == {"archive_job_id": str(archive_job.id)}


@requires_infra
def test_create_archive_rollback_deletes_the_object_and_marks_archive_job_failed(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(
        db, organization_id=user.organization_id, user_id=user.id, granted_scopes=DRIVE_WRITE_SCOPE
    )
    file = _provision_file(db, connector_id=connector.id, name="A.txt", provider_file_id="f-a")
    plan = _provision_plan(db, organization_id=user.organization_id, user_id=user.id)
    step = _provision_step(
        db, plan_id=plan.id, file_id=file.id, action_type=ExecutionActionType.CREATE_ARCHIVE
    )
    forward_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id
    )

    storage = _FakeObjectStorageClient()
    service = ExecutionService(
        db,
        storage=build_storage_registry(
            db,
            oauth_client=_FakeGoogleWorkspaceOAuthClient(),
            drive_client=_FakeGoogleDriveClient(),
        ),
        object_storage_client=storage,
    )
    service.run(forward_job.id)

    archive_job = ArchiveJobRepository(db).get_by_plan_id(plan.id)
    assert archive_job.status == ArchiveJobStatus.COMPLETED
    uploaded_key = archive_job.object_storage_key
    assert uploaded_key in storage.put_calls

    rollback_job = _provision_job(
        db, plan_id=plan.id, organization_id=user.organization_id, user_id=user.id, is_rollback=True
    )
    service.run(rollback_job.id)

    completed_rollback_job = ExecutionJobRepository(db).get_by_id(rollback_job.id)
    assert completed_rollback_job.status == ExecutionJobStatus.COMPLETED

    assert uploaded_key in storage.delete_calls
    rolled_back_archive_job = ArchiveJobRepository(db).get_by_id(archive_job.id)
    assert rolled_back_archive_job.status == ArchiveJobStatus.FAILED

    rolled_back_step = ExecutionStepRepository(db).get_by_id(step.id)
    assert rolled_back_step.status == ExecutionStepStatus.ROLLED_BACK
