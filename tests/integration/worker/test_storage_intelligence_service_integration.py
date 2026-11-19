import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session
from vault_shared import get_settings
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    RoleName,
    StorageAnalysisTrigger,
)
from vault_shared.db.repositories import (
    DuplicateGroupRepository,
    FileRepository,
    OrganizationRepository,
    RoleRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from worker.storage_intelligence.storage_intelligence_service import (
    StorageIntelligenceService,
)


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
    size_bytes: int | None = 1024,
    mime_type: str = "text/plain",
    checksum: str | None = None,
    modified_at: datetime | None = None,
    viewed_at: datetime | None = None,
    is_shared: bool = False,
    owner_email: str = "founder@acme.com",
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id, provider_drive_id="root", name="My Drive", drive_type=DriveType.MY_DRIVE
    )
    now = modified_at if modified_at is not None else datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=provider_file_id,
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type=mime_type,
        size_bytes=size_bytes,
        owner_email=owner_email,
        is_shared=is_shared,
        permissions_summary=None,
        version_id=None,
        checksum=checksum,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=modified_at,
        provider_viewed_at=viewed_at,
        scanned_at=datetime.now(UTC),
    )
    db.commit()
    return file


def _create_job(db: Session, *, organization_id: uuid.UUID):
    job = StorageAnalysisJobRepository(db).create(
        organization_id=organization_id,
        triggered_by=StorageAnalysisTrigger.MANUAL,
        triggered_by_user_id=None,
    )
    db.commit()
    return job


@requires_infra
def test_identical_content_files_are_grouped_as_exact_duplicates(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="report.pdf", provider_file_id="f-1",
        size_bytes=10_000_000, checksum="abc123",
    )
    _provision_file(
        db, connector_id=connector.id, name="report (1).pdf", provider_file_id="f-2",
        size_bytes=10_000_000, checksum="abc123",
    )
    _provision_file(
        db, connector_id=connector.id, name="backup/report.pdf", provider_file_id="f-3",
        size_bytes=10_000_000, checksum="abc123",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    groups, total = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total == 1
    group = groups[0]
    assert group.file_count == 3
    assert group.total_size_bytes == 30_000_000
    # The retained file is never counted as recoverable (spec §6.4).
    assert group.recoverable_size_bytes == 20_000_000
    assert group.recommended_keep_file_id is not None


@requires_infra
def test_same_name_different_content_is_not_a_duplicate(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="report.pdf", provider_file_id="f-1", checksum="aaa"
    )
    _provision_file(
        db, connector_id=connector.id, name="report.pdf", provider_file_id="f-2", checksum="bbb"
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    _, total = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total == 0


@requires_infra
def test_files_with_no_checksum_are_never_treated_as_duplicates(db: Session) -> None:
    """Google-native files (Docs/Sheets/Slides) have no md5Checksum (spec
    §6.7) — a shared `None` checksum must never be treated as a match."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="Doc A", provider_file_id="f-1", checksum=None,
        mime_type="application/vnd.google-apps.document",
    )
    _provision_file(
        db, connector_id=connector.id, name="Doc B", provider_file_id="f-2", checksum=None,
        mime_type="application/vnd.google-apps.document",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    _, total = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total == 0


@requires_infra
def test_zero_byte_files_with_matching_checksum_are_still_grouped(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="empty1.txt", provider_file_id="f-1",
        size_bytes=0, checksum="empty-hash",
    )
    _provision_file(
        db, connector_id=connector.id, name="empty2.txt", provider_file_id="f-2",
        size_bytes=0, checksum="empty-hash",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    groups, total = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total == 1
    assert groups[0].total_size_bytes == 0
    assert groups[0].recoverable_size_bytes == 0


@requires_infra
def test_duplicate_groups_never_cross_organizations(db: Session) -> None:
    user_a = _provision_user(db)
    connector_a = _provision_connector(db, organization_id=user_a.organization_id, user_id=user_a.id)
    _provision_file(
        db, connector_id=connector_a.id, name="shared-template.docx", provider_file_id="f-1",
        checksum="same-hash-both-orgs",
    )
    _provision_file(
        db, connector_id=connector_a.id, name="shared-template-2.docx", provider_file_id="f-2",
        checksum="same-hash-both-orgs",
    )

    user_b = _provision_user(db)
    connector_b = _provision_connector(db, organization_id=user_b.organization_id, user_id=user_b.id)
    _provision_file(
        db, connector_id=connector_b.id, name="shared-template.docx", provider_file_id="f-3",
        checksum="same-hash-both-orgs",
    )
    _provision_file(
        db, connector_id=connector_b.id, name="shared-template-2.docx", provider_file_id="f-4",
        checksum="same-hash-both-orgs",
    )

    StorageIntelligenceService(db).run(_create_job(db, organization_id=user_a.organization_id).id)
    StorageIntelligenceService(db).run(_create_job(db, organization_id=user_b.organization_id).id)

    groups_a, total_a = DuplicateGroupRepository(db).list_for_organization(
        user_a.organization_id, limit=10, offset=0
    )
    groups_b, total_b = DuplicateGroupRepository(db).list_for_organization(
        user_b.organization_id, limit=10, offset=0
    )
    assert total_a == 1
    assert total_b == 1
    assert groups_a[0].id != groups_b[0].id
    assert groups_a[0].file_count == 2
    assert groups_b[0].file_count == 2


@requires_infra
def test_rerunning_analysis_is_idempotent_and_keeps_the_same_group_id(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="a.pdf", provider_file_id="f-1", checksum="dup-x"
    )
    _provision_file(
        db, connector_id=connector.id, name="b.pdf", provider_file_id="f-2", checksum="dup-x"
    )

    StorageIntelligenceService(db).run(_create_job(db, organization_id=user.organization_id).id)
    groups_first, total_first = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )

    StorageIntelligenceService(db).run(_create_job(db, organization_id=user.organization_id).id)
    groups_second, total_second = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )

    assert total_first == 1
    assert total_second == 1
    assert groups_first[0].id == groups_second[0].id


@requires_infra
def test_a_group_that_no_longer_has_duplicates_is_removed_on_rerun(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file_a = _provision_file(
        db, connector_id=connector.id, name="a.pdf", provider_file_id="f-1", checksum="dup-y"
    )
    _provision_file(
        db, connector_id=connector.id, name="b.pdf", provider_file_id="f-2", checksum="dup-y"
    )
    StorageIntelligenceService(db).run(_create_job(db, organization_id=user.organization_id).id)
    _, total_before = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total_before == 1

    # The checksum changes (e.g. file content changed on a rescan) so only
    # one file has it now — no longer a duplicate group.
    file_a.checksum = "no-longer-shared"
    db.commit()

    StorageIntelligenceService(db).run(_create_job(db, organization_id=user.organization_id).id)
    _, total_after = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total_after == 0


@requires_infra
def test_storage_analysis_snapshot_totals_and_breakdowns(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="video.mp4", provider_file_id="f-1",
        size_bytes=6_000_000_000, mime_type="video/mp4",
    )
    _provision_file(
        db, connector_id=connector.id, name="doc.pdf", provider_file_id="f-2",
        size_bytes=500_000, mime_type="application/pdf",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    snapshot = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user.organization_id
    )
    assert snapshot is not None
    assert snapshot.total_files == 2
    assert snapshot.total_size_bytes == 6_000_500_000
    assert snapshot.breakdown_by_type_bytes["Videos"] == 6_000_000_000
    assert snapshot.breakdown_by_type_bytes["Documents"] == 500_000
    assert snapshot.breakdown_by_size_bucket_bytes["> 5 GB"] == 6_000_000_000
    assert snapshot.large_file_count == 1  # the 6GB video only — 500KB doc is not "large"


@requires_infra
def test_files_shared_by_someone_else_are_excluded_from_storage_totals(db: Session) -> None:
    """A file shared with the connected account by another owner doesn't
    count against *this* account's real Drive quota (only owned files do),
    and ExecutionService can never trash it (insufficientFilePermissions —
    confirmed in production). Counting it toward "storage used" both
    overstates the number and offers a cleanup action guaranteed to fail."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="mine.pdf", provider_file_id="f-owned",
        size_bytes=500_000, owner_email="founder@acme.com",
    )
    _provision_file(
        db, connector_id=connector.id, name="shared-with-me.zip", provider_file_id="f-shared",
        size_bytes=50_000_000, owner_email="someone.else@gmail.com",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    snapshot = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user.organization_id
    )
    assert snapshot.total_files == 1
    assert snapshot.total_size_bytes == 500_000


@requires_infra
def test_a_duplicate_where_only_one_copy_is_owned_is_not_flagged(db: Session) -> None:
    """Same reasoning as the storage-total exclusion above: a "duplicate"
    where the second copy belongs to someone else isn't something this
    account can actually clean up, so it shouldn't be surfaced as one."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="report.pdf", provider_file_id="f-owned",
        size_bytes=10_000_000, checksum="abc123", owner_email="founder@acme.com",
    )
    _provision_file(
        db, connector_id=connector.id, name="report (shared copy).pdf", provider_file_id="f-shared",
        size_bytes=10_000_000, checksum="abc123", owner_email="someone.else@gmail.com",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    _, total = DuplicateGroupRepository(db).list_for_organization(
        user.organization_id, limit=10, offset=0
    )
    assert total == 0


@requires_infra
def test_old_and_inactive_files_are_detected_from_real_timestamps(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    two_years_ago = datetime.now(UTC) - timedelta(days=730)
    _provision_file(
        db, connector_id=connector.id, name="stale.pdf", provider_file_id="f-1",
        size_bytes=1000, modified_at=two_years_ago, viewed_at=two_years_ago,
    )
    _provision_file(
        db, connector_id=connector.id, name="fresh.pdf", provider_file_id="f-2",
        size_bytes=1000, modified_at=datetime.now(UTC),
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    snapshot = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user.organization_id
    )
    assert snapshot.old_file_count == 1
    assert snapshot.inactive_file_count == 1


@requires_infra
def test_temporary_candidate_and_duplicate_savings_are_not_double_counted(db: Session) -> None:
    """A file that is both a redundant duplicate AND name-matches a
    temporary-file pattern must be counted once in total savings, not
    twice (spec §12)."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db, connector_id=connector.id, name="report.pdf", provider_file_id="f-1",
        size_bytes=1_000_000, checksum="dup-z",
    )
    # Redundant duplicate copy whose name ALSO matches the temp-file
    # heuristic — must only contribute once to total_potential_savings.
    _provision_file(
        db, connector_id=connector.id, name="report backup.pdf", provider_file_id="f-2",
        size_bytes=1_000_000, checksum="dup-z",
    )
    job = _create_job(db, organization_id=user.organization_id)

    StorageIntelligenceService(db).run(job.id)

    snapshot = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user.organization_id
    )
    assert snapshot.duplicate_recoverable_bytes == 1_000_000
    assert snapshot.temporary_candidate_bytes == 1_000_000
    # NOT 2_000_000 — the one overlapping file counts once.
    assert snapshot.total_potential_savings_bytes == 1_000_000


@requires_infra
def test_analysis_is_organization_scoped_and_isolated(db: Session) -> None:
    user_a = _provision_user(db)
    connector_a = _provision_connector(db, organization_id=user_a.organization_id, user_id=user_a.id)
    _provision_file(db, connector_id=connector_a.id, name="a.pdf", provider_file_id="f-1", size_bytes=1000)

    user_b = _provision_user(db)
    connector_b = _provision_connector(db, organization_id=user_b.organization_id, user_id=user_b.id)
    for i in range(3):
        _provision_file(
            db, connector_id=connector_b.id, name=f"b{i}.pdf", provider_file_id=f"f-b-{i}", size_bytes=2000
        )

    StorageIntelligenceService(db).run(_create_job(db, organization_id=user_a.organization_id).id)

    snapshot_a = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user_a.organization_id
    )
    snapshot_b = StorageAnalysisSnapshotRepository(db).get_latest_for_organization(
        user_b.organization_id
    )
    assert snapshot_a is not None
    assert snapshot_a.total_files == 1
    # Organization B was never analyzed — no snapshot exists for it yet,
    # proving analyzing A never touched or leaked B's data.
    assert snapshot_b is None


def _provision_local_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.LOCAL_AGENT,
        connected_by_user_id=user_id,
        account_email=None,
        workspace_domain=None,
    )
    db.commit()
    return connector


def _provision_local_file(db: Session, *, connector_id: uuid.UUID, name: str, size: int, checksum: str):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root-id",
        name="C:\Projects",
        drive_type=DriveType.LOCAL_FOLDER,
    )
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=f"local-{name}",
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="video/mp4",
        size_bytes=size,
        owner_email=None,
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=checksum,
        web_view_link=None,
        provider_created_at=datetime.now(UTC),
        provider_modified_at=datetime.now(UTC),
        provider_viewed_at=None,
        scanned_at=datetime.now(UTC),
    )
    db.commit()
    return file


@pytest.fixture
def two_storages(db: Session):
    user = _provision_user(db)
    drive = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    computer = _provision_local_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=drive.id, name="brief.txt", provider_file_id="d-1", size_bytes=1_000)
    _provision_local_file(db, connector_id=computer.id, name="clip.mp4", size=500_000_000, checksum="v1")
    _provision_local_file(db, connector_id=computer.id, name="clip copy.mp4", size=500_000_000, checksum="v1")
    StorageIntelligenceService(db).run(_create_job(db, organization_id=user.organization_id).id)
    return user, drive, computer


@requires_infra
def test_each_connected_storage_gets_its_own_snapshot(db: Session, two_storages) -> None:  # noqa: ANN001
    user, drive, computer = two_storages
    snapshots = StorageAnalysisSnapshotRepository(db)

    whole = snapshots.get_latest_for_organization(user.organization_id)
    on_drive = snapshots.get_latest_for_organization(user.organization_id, connector_id=drive.id)
    on_computer = snapshots.get_latest_for_organization(user.organization_id, connector_id=computer.id)

    assert (whole.total_files, on_drive.total_files, on_computer.total_files) == (3, 1, 2)
    assert on_drive.total_size_bytes == 1_000
    assert on_computer.total_size_bytes == 1_000_000_000


@requires_infra
def test_a_duplicate_counts_in_the_storage_that_holds_the_copy(db: Session, two_storages) -> None:  # noqa: ANN001
    user, drive, computer = two_storages
    snapshots = StorageAnalysisSnapshotRepository(db)

    on_drive = snapshots.get_latest_for_organization(user.organization_id, connector_id=drive.id)
    on_computer = snapshots.get_latest_for_organization(user.organization_id, connector_id=computer.id)

    assert (on_drive.duplicate_group_count, on_drive.duplicate_recoverable_bytes) == (0, 0)
    assert (on_computer.duplicate_group_count, on_computer.duplicate_recoverable_bytes) == (1, 500_000_000)
    assert on_computer.total_potential_savings_bytes >= 500_000_000


@requires_infra
def test_lists_can_be_narrowed_to_one_storage(db: Session, two_storages) -> None:  # noqa: ANN001
    user, drive, computer = two_storages
    files = FileRepository(db)
    groups = DuplicateGroupRepository(db)

    large_on_drive, _ = files.list_large_for_organization(
        user.organization_id, min_size_bytes=100, limit=10, offset=0, connector_id=drive.id
    )
    large_on_computer, _ = files.list_large_for_organization(
        user.organization_id, min_size_bytes=100, limit=10, offset=0, connector_id=computer.id
    )
    _, drive_groups = groups.list_for_organization(
        user.organization_id, limit=10, offset=0, connector_id=drive.id
    )
    _, computer_groups = groups.list_for_organization(
        user.organization_id, limit=10, offset=0, connector_id=computer.id
    )

    assert [f.name for f in large_on_drive] == ["brief.txt"]
    assert {f.name for f in large_on_computer} == {"clip.mp4", "clip copy.mp4"}
    assert (drive_groups, computer_groups) == (0, 1)
