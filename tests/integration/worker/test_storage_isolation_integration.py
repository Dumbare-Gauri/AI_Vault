"""At the mutation point, a plan acts only on files of its own storage: a
Google Drive plan never touches a local file, and a plan for this computer
never reaches Google Drive — even if a step naming the other storage's file
were planted directly. Fake Drive only; no real storage is touched."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_execution_hardening_integration import _mutations, _org_with_file, _run, _step_status
from test_execution_service_integration import (
    _drive_file,
    _FakeGoogleDriveClient,
    requires_infra,
)
from test_recommendation_service_integration import (
    _create_job,
    _provision_connector,
    _provision_file,
    _provision_user,
)

from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    ExecutionActionType,
    ExecutionStepStatus,
)
from vault_shared.db.repositories import (
    ExecutionPlanRepository,
    ExecutionStepRepository,
    FileRepository,
    RecommendationRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory
from worker.recommendation.recommendation_service import RecommendationService


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _local_file(db: Session, *, user, name: str):  # noqa: ANN001, ANN202
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=user.organization_id,
        provider=ConnectorProvider.LOCAL_AGENT,
        connected_by_user_id=user.id,
        account_email=None,
        workspace_domain=None,
    )
    source = StorageSourceRepository(db).upsert(
        connector_id=connector.id,
        provider_drive_id="disk",
        name="C:\\Work",
        drive_type=DriveType.LOCAL_FOLDER,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        # The same provider id the Drive file has — identity is never the id alone.
        provider_file_id="f-1",
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=100,
        owner_email=None,
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


def _trash_step(db: Session, *, plan, file):  # noqa: ANN001, ANN202
    step = ExecutionStepRepository(db).create(
        execution_plan_id=plan.id,
        step_order=0,
        action_type=ExecutionActionType.ARCHIVE,
        target_file_id=file.id,
        pre_state={},
        planned_change={"action": ExecutionActionType.ARCHIVE},
    )
    db.commit()
    return step


@requires_infra
def test_a_drive_plan_never_acts_on_a_local_file(db: Session) -> None:
    user, _drive_twin, plan = _org_with_file(db, provider_file_id="f-1", name="Report.pdf")
    local = _local_file(db, user=user, name="Report.pdf")
    step = _trash_step(db, plan=plan, file=local)
    drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Report.pdf")})

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) == ExecutionStepStatus.FAILED
    assert _mutations(drive) == []


@requires_infra
def test_a_local_plan_never_reaches_google_drive(db: Session) -> None:
    user, drive_file, plan = _org_with_file(db, provider_file_id="f-1", name="Report.pdf")
    _local_file(db, user=user, name="Report.pdf")
    ExecutionPlanRepository(db).get_by_id(plan.id).target_provider = ConnectorProvider.LOCAL_AGENT
    db.commit()
    step = _trash_step(db, plan=plan, file=drive_file)
    drive = _FakeGoogleDriveClient(files={"f-1": _drive_file(file_id="f-1", name="Report.pdf")})

    _run(db, drive, plan=plan, user=user)

    assert _step_status(db, step) != ExecutionStepStatus.COMPLETED
    assert _mutations(drive) == []


@requires_infra
def test_recommendations_are_computed_per_storage_and_never_mix_files(db: Session) -> None:
    user = _provision_user(db)
    drive = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    local = StorageConnectorRepository(db).upsert_connected(
        organization_id=user.organization_id,
        provider=ConnectorProvider.LOCAL_AGENT,
        connected_by_user_id=user.id,
        account_email=None,
        workspace_domain=None,
    )
    db.commit()
    files = {}
    for connector, where in ((drive, "drive"), (local, "local")):
        for i in range(6):
            _provision_file(
                db,
                connector_id=connector.id,
                name=f"Filler{i}.txt",
                provider_file_id=f"{where}-{i}",
            )
        # The same duplicate key in both storages: still two separate findings.
        files[where] = {
            _provision_file(
                db,
                connector_id=connector.id,
                name=f"{copy}.pdf",
                provider_file_id=f"{where}-{copy}",
                duplicate_group_key="same-key",
            ).id
            for copy in ("A", "B")
        }

    RecommendationService(db).run(_create_job(db, organization_id=user.organization_id).id)

    repo = RecommendationRepository(db)
    for connector, where in ((drive, "drive"), (local, "local")):
        found = repo.get_by_rule(
            organization_id=user.organization_id,
            rule_name="duplicate_files",
            connector_id=connector.id,
        )
        assert found is not None
        affected = {uuid.UUID(file_id) for file_id in found.affected_file_ids}
        assert affected and affected <= files[where]
