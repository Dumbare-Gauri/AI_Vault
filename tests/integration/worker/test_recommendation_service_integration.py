import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session
from vault_shared import get_settings
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    RecommendationJobStatus,
    RecommendationStatus,
    RecommendationTrigger,
    RoleName,
)
from vault_shared.db.repositories import (
    DashboardSnapshotRepository,
    FileMetadataRepository,
    FileRepository,
    OrganizationRepository,
    RecommendationJobRepository,
    RecommendationRepository,
    RoleRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from worker.recommendation.recommendation_service import RecommendationService


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


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    db.commit()
    return connector


def _provision_file(
    db: Session,
    *,
    connector_id: uuid.UUID,
    name: str,
    provider_file_id: str,
    size_bytes: int = 1024,
    is_shared: bool = False,
    owner_email: str = "founder@acme.com",
    modified_at: datetime | None = None,
    duplicate_group_key: str | None = None,
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id, provider_drive_id="root", name="My Drive", drive_type=DriveType.MY_DRIVE
    )
    now = modified_at or datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=provider_file_id,
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=size_bytes,
        owner_email=owner_email,
        is_shared=is_shared,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=datetime.now(UTC),
    )
    if duplicate_group_key:
        FileMetadataRepository(db).upsert(
            file_id=file.id,
            normalized_extension="txt",
            mime_type_validated=True,
            mime_mismatch_reason=None,
            naming_pattern=None,
            version_label=None,
            owner_summary=owner_email,
            sharing_summary="shared" if is_shared else "private",
            language="en",
            enriched_at=datetime.now(UTC),
        )
        FileMetadataRepository(db).set_duplicate_group_key(file.id, duplicate_group_key)
    db.commit()
    return file


def _create_job(db: Session, *, organization_id: uuid.UUID):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id,
        triggered_by=RecommendationTrigger.MANUAL,
        triggered_by_user_id=None,
    )
    db.commit()
    return job


@requires_infra
def test_recommendation_run_persists_a_duplicate_files_recommendation(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    for i in range(6):
        _provision_file(db, connector_id=connector.id, name=f"Filler{i}.txt", provider_file_id=f"f-filler-{i}")
    _provision_file(
        db,
        connector_id=connector.id,
        name="A.pdf",
        provider_file_id="f-a",
        size_bytes=500,
        duplicate_group_key="dup-1",
    )
    _provision_file(
        db,
        connector_id=connector.id,
        name="B.pdf",
        provider_file_id="f-b",
        size_bytes=800,
        duplicate_group_key="dup-1",
    )
    job = _create_job(db, organization_id=user.organization_id)

    RecommendationService(db).run(job.id)

    completed_job = RecommendationJobRepository(db).get_by_id(job.id)
    assert completed_job.status == RecommendationJobStatus.COMPLETED

    recommendation = RecommendationRepository(db).get_by_rule(
        organization_id=user.organization_id, rule_name="duplicate_files"
    )
    assert recommendation is not None
    assert recommendation.status == RecommendationStatus.ACTIVE
    assert recommendation.impact_value == 500.0


@requires_infra
def test_recommendation_run_persists_a_dashboard_snapshot(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    for i in range(6):
        _provision_file(db, connector_id=connector.id, name=f"F{i}.txt", provider_file_id=f"f-{i}")
    job = _create_job(db, organization_id=user.organization_id)

    RecommendationService(db).run(job.id)

    snapshot = DashboardSnapshotRepository(db).get_latest_for_organization(user.organization_id)
    assert snapshot is not None
    assert snapshot.total_files == 6
    assert snapshot.connected_providers == 1


@requires_infra
def test_dashboard_snapshot_totals_exclude_files_owned_by_someone_else(db: Session) -> None:
    """A file shared with the connected account by another owner never
    counted against this account's real Drive quota, and can't be trashed
    (insufficientFilePermissions) — so it shouldn't count toward the
    Dashboard's total_files/total_storage_bytes either. The ownership rules
    themselves (OrphanedOwnershipRule etc., tested separately in
    test_recommendation_rules.py) still need to see it, which is why this
    exclusion lives only in the snapshot totals, not the underlying query."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    for i in range(6):
        _provision_file(db, connector_id=connector.id, name=f"F{i}.txt", provider_file_id=f"f-{i}")
    _provision_file(
        db, connector_id=connector.id, name="shared-with-me.zip", provider_file_id="f-shared",
        size_bytes=50_000_000, owner_email="someone.else@gmail.com",
    )
    job = _create_job(db, organization_id=user.organization_id)

    RecommendationService(db).run(job.id)

    snapshot = DashboardSnapshotRepository(db).get_latest_for_organization(user.organization_id)
    assert snapshot.total_files == 6
    assert snapshot.total_storage_bytes == 6 * 1024
    assert snapshot.classified_files + snapshot.unclassified_files == snapshot.total_files


@requires_infra
def test_a_second_run_resolves_a_recommendation_whose_condition_no_longer_holds(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    for i in range(6):
        _provision_file(db, connector_id=connector.id, name=f"Filler{i}.txt", provider_file_id=f"f-filler-{i}")
    a = _provision_file(
        db,
        connector_id=connector.id,
        name="A.pdf",
        provider_file_id="f-a",
        duplicate_group_key="dup-1",
    )
    b = _provision_file(
        db,
        connector_id=connector.id,
        name="B.pdf",
        provider_file_id="f-b",
        duplicate_group_key="dup-1",
    )
    first_job = _create_job(db, organization_id=user.organization_id)
    RecommendationService(db).run(first_job.id)

    recommendations = RecommendationRepository(db)
    active = recommendations.get_by_rule(
        organization_id=user.organization_id, rule_name="duplicate_files", connector_id=connector.id
    )
    assert active is not None
    assert active.status == RecommendationStatus.ACTIVE

    # The duplicate condition no longer holds (group key cleared on both
    # files) — a fresh run must resolve the previously-active recommendation
    # rather than leaving it stranded as ACTIVE forever.
    FileMetadataRepository(db).set_duplicate_group_key(a.id, None)
    FileMetadataRepository(db).set_duplicate_group_key(b.id, None)
    db.commit()

    second_job = _create_job(db, organization_id=user.organization_id)
    RecommendationService(db).run(second_job.id)

    resolved = recommendations.get_by_rule(
        organization_id=user.organization_id, rule_name="duplicate_files", connector_id=connector.id
    )
    assert resolved is not None
    assert resolved.status == RecommendationStatus.RESOLVED
    assert resolved.resolved_at is not None


@requires_infra
def test_recommendation_run_skips_rules_for_a_near_empty_organization(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Only.txt", provider_file_id="f-only")
    job = _create_job(db, organization_id=user.organization_id)

    RecommendationService(db).run(job.id)

    completed_job = RecommendationJobRepository(db).get_by_id(job.id)
    assert completed_job.status == RecommendationJobStatus.COMPLETED
    assert completed_job.recommendations_active == 0
