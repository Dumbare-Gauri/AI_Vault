import socket
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, ReauthRequiredError, get_settings
from vault_shared.connectors.google_drive import (
    DriveChangesPage,
    DriveFile,
    DriveFilesPage,
    SharedDrive,
)
from vault_shared.connectors.google_workspace import GoogleAccountInfo, GoogleTokenSet
from vault_shared.db.models import (
    ConnectorProvider,
    ConnectorStatus,
    RoleName,
    ScanStatus,
    ScanType,
)
from vault_shared.db.repositories import (
    ConnectorCredentialsRepository,
    FileRepository,
    FolderRepository,
    OrganizationRepository,
    RoleRepository,
    ScanJobRepository,
    ScanProgressRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import encrypt_token
from vault_shared.storage.default_registry import build_storage_registry
from worker.scanner.scan_service import ScannerService


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
    """No real Google credentials in this sandbox — stands in exactly where
    GoogleWorkspaceOAuthClient would via constructor injection, matching
    Phase 3's connector integration test pattern."""

    def exchange_code(self, *, code: str) -> GoogleTokenSet:
        return GoogleTokenSet(
            access_token="access-1",
            refresh_token="refresh-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        )

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        return GoogleTokenSet(
            access_token="access-refreshed",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        )

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        return GoogleAccountInfo(email="founder@acme.com", workspace_domain="acme.com")

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def revoke(self, *, token: str) -> bool:
        return True


class _FakeGoogleDriveClient:
    """Canned Drive responses, configured per test — never calls the real
    Google Drive API. `files_pages` maps a drive_id (None for My Drive) to a
    list of `DriveFilesPage`s returned in order across successive calls."""

    def __init__(
        self,
        *,
        shared_drives: list[SharedDrive] | None = None,
        files_pages: dict[str | None, list[DriveFilesPage]] | None = None,
        changes: DriveChangesPage | None = None,
    ) -> None:
        self._changes = changes
        self.shared_drives = shared_drives or []
        self._files_pages = {k: list(v) for k, v in (files_pages or {}).items()}
        self.list_files_page_calls = 0
        self._on_call: Callable[[], None] | None = None

    def list_shared_drives(self, *, access_token: str) -> list[SharedDrive]:
        return self.shared_drives

    def list_files_page(
        self,
        *,
        access_token: str,
        drive_id: str | None,
        page_token: str | None,
        page_size: int = 1000,
    ) -> DriveFilesPage:
        self.list_files_page_calls += 1
        if self._on_call:
            self._on_call()
        pages = self._files_pages.get(drive_id, [])
        index = 0 if page_token is None else int(page_token)
        page = pages[index]
        next_token = str(index + 1) if index + 1 < len(pages) else None
        return DriveFilesPage(files=page.files, next_page_token=next_token)

    def get_start_page_token(self, *, access_token: str, drive_id: str | None) -> str:
        return "start-token-1"

    def list_changes_page(self, *, access_token: str, page_token: str, drive_id: str | None):
        if self._changes is None:
            raise NotImplementedError
        return self._changes


def _folder(item_id: str, name: str, *, parent_id: str | None) -> DriveFile:
    return DriveFile(
        id=item_id,
        name=name,
        mime_type="application/vnd.google-apps.folder",
        parents=[parent_id] if parent_id else [],
        size=None,
        created_time=None,
        modified_time=None,
        viewed_by_me_time=None,
        owner_email="founder@acme.com",
        shared=False,
        checksum=None,
        version_id=None,
        is_folder=True,
        trashed=False,
    )


def _file(
    item_id: str, name: str, *, parent_id: str | None, web_view_link: str | None = None
) -> DriveFile:
    return DriveFile(
        id=item_id,
        name=name,
        mime_type="application/pdf",
        parents=[parent_id] if parent_id else [],
        size=1024,
        created_time=None,
        modified_time=None,
        viewed_by_me_time=None,
        owner_email="founder@acme.com",
        shared=False,
        checksum="abc123",
        version_id="v1",
        is_folder=False,
        trashed=False,
        web_view_link=web_view_link,
    )


def _provision_user(db: Session):
    """Provisions a persisted Organization+Role+User directly via
    `vault_shared` repositories rather than the backend's `AuthService` —
    apps/backend and apps/worker are independently deployable (ADR-012), so
    a worker-side test can't import backend application code."""
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
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("access-1"),
        refresh_token_encrypted=encrypt_token("refresh-1"),
        granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return connector


def _create_job(db: Session, *, connector_id: uuid.UUID, scan_type: str = ScanType.FULL):
    job = ScanJobRepository(db).create(
        connector_id=connector_id, scan_type=scan_type, triggered_by_user_id=None
    )
    db.commit()
    return job


@requires_infra
def test_full_scan_ingests_folders_and_files_with_correct_materialized_paths(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(
        files_pages={
            None: [
                DriveFilesPage(
                    files=[
                        _folder("f-reports", "Reports", parent_id=None),
                        _file("file-q1", "Q1.pdf", parent_id="f-reports"),
                    ],
                    next_page_token=None,
                )
            ]
        }
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    completed_job = ScanJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ScanStatus.COMPLETED

    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    folder = FolderRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="f-reports"
    )
    file = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q1"
    )
    assert folder.path == "/Reports"
    assert file.path == "/Reports/Q1.pdf"
    assert file.parent_folder_id == folder.id

    progress = ScanProgressRepository(db).get_for_job(job.id)
    assert progress.folders_discovered == 1
    assert progress.files_discovered == 1
    assert progress.sources_completed == 1


@requires_infra
def test_full_scan_resolves_hierarchy_even_when_child_arrives_before_parent(db: Session) -> None:
    """Drive's files.list pagination gives no ordering guarantee — this feeds
    the child folder and file in the same page as the parent, in reverse
    order, to prove the two-pass resolve step (not ingest order) determines
    the final materialized path."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(
        files_pages={
            None: [
                DriveFilesPage(
                    files=[
                        _file("file-old", "old.pdf", parent_id="f-archive"),
                        _folder("f-archive", "Archive", parent_id="f-reports"),
                        _folder("f-reports", "Reports", parent_id=None),
                    ],
                    next_page_token=None,
                )
            ]
        }
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    archive = FolderRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="f-archive"
    )
    old_file = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-old"
    )
    assert archive.path == "/Reports/Archive"
    assert old_file.path == "/Reports/Archive/old.pdf"


@requires_infra
def test_full_scan_persists_the_provider_web_view_link(db: Session) -> None:
    """Step 5/8 of the V1 vertical slice: the File Explorer's "open the
    provider file" affordance needs Drive's own `webViewLink`, not a URL we
    construct ourselves — this proves it survives ingest onto `File.web_view_link`
    unchanged."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    link = "https://drive.google.com/file/d/file-q1/view?usp=drivesdk"
    drive = _FakeGoogleDriveClient(
        files_pages={
            None: [
                DriveFilesPage(
                    files=[_file("file-q1", "Q1.pdf", parent_id=None, web_view_link=link)],
                    next_page_token=None,
                )
            ]
        }
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    file = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q1"
    )
    assert file.web_view_link == link


@requires_infra
def test_full_scan_ingests_across_multiple_drive_api_pages(db: Session) -> None:
    """Drive's files.list caps each response at a page size, so a source
    with more items than fit on one page must be walked across successive
    `nextPageToken` calls. Earlier tests only ever configure a single page
    per source; this proves `_ingest_full`'s `while True` loop actually
    follows `next_page_token` rather than stopping after the first page."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(
        files_pages={
            None: [
                DriveFilesPage(
                    files=[_folder("f-reports", "Reports", parent_id=None)],
                    next_page_token="1",
                ),
                DriveFilesPage(
                    files=[_file("file-q1", "Q1.pdf", parent_id="f-reports")],
                    next_page_token="2",
                ),
                DriveFilesPage(
                    files=[_file("file-q2", "Q2.pdf", parent_id="f-reports")],
                    next_page_token=None,
                ),
            ]
        }
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    assert drive.list_files_page_calls == 3

    completed_job = ScanJobRepository(db).get_by_id(job.id)
    assert completed_job.status == ScanStatus.COMPLETED

    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    q1 = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q1"
    )
    q2 = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q2"
    )
    assert q1.path == "/Reports/Q1.pdf"
    assert q2.path == "/Reports/Q2.pdf"

    progress = ScanProgressRepository(db).get_for_job(job.id)
    assert progress.folders_discovered == 1
    assert progress.files_discovered == 2


@requires_infra
def test_cancellation_between_sources_stops_the_scan_cleanly(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(
        shared_drives=[SharedDrive(id="shared-1", name="Team Drive")],
        files_pages={
            None: [
                DriveFilesPage(
                    files=[_folder("f1", "Folder1", parent_id=None)], next_page_token=None
                )
            ],
            "shared-1": [DriveFilesPage(files=[], next_page_token=None)],
        },
    )
    jobs = ScanJobRepository(db)

    def _cancel_after_my_drive() -> None:
        if drive.list_files_page_calls == 1:
            jobs.request_cancel(job)
            db.commit()

    drive._on_call = _cancel_after_my_drive
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    cancelled_job = jobs.get_by_id(job.id)
    assert cancelled_job.status == ScanStatus.CANCELLED
    # My Drive's single page was ingested before cancellation was observed.
    assert drive.list_files_page_calls == 1


@requires_infra
def test_dependency_unavailable_leaves_the_job_running_for_a_celery_level_retry(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient()

    def _raise() -> None:
        raise DependencyUnavailableError("Google Drive rate limit exceeded.")

    drive._on_call = _raise
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    with pytest.raises(DependencyUnavailableError):
        service.run(job.id)

    still_running_job = ScanJobRepository(db).get_by_id(job.id)
    assert still_running_job.status == ScanStatus.RUNNING


@requires_infra
def test_revoked_refresh_token_fails_the_scan_without_leaking_the_token(db: Session) -> None:
    """A revoked/expired connector (Step 11 of the V1 vertical slice, and the
    real-world root cause behind the "token refresh failed" scan blocker) —
    the stored access token is already past expiry, forcing
    `ConnectorTokenService` down its refresh path, and Google rejects the
    refresh with `invalid_grant`. Unlike `DependencyUnavailableError`
    (transient, retryable), this is a permanent auth failure: the job must
    be marked FAILED outright — not left RUNNING for a Celery retry that can
    never succeed — the connector itself must flip to REAUTH_REQUIRED (not
    silently stay "connected" while every future scan keeps failing the
    same way), and the persisted error must describe the failure without
    ever containing the token itself."""
    user = _provision_user(db)
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=user.organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user.id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    secret_refresh_token = "refresh-token-secret-value"
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("access-expired"),
        refresh_token_encrypted=encrypt_token(secret_refresh_token),
        granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.commit()
    job = _create_job(db, connector_id=connector.id)

    class _RevokedOAuthClient(_FakeGoogleWorkspaceOAuthClient):
        def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
            raise ReauthRequiredError(
                "Google authorization has expired or was revoked. Reconnect Google Drive to continue."
            )

    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_RevokedOAuthClient(), drive_client=_FakeGoogleDriveClient()
        ),
    )

    service.run(job.id)

    failed_job = ScanJobRepository(db).get_by_id(job.id)
    assert failed_job.status == ScanStatus.FAILED
    assert secret_refresh_token not in (failed_job.error or "")

    failed_connector = StorageConnectorRepository(db).get_by_id(connector.id)
    assert failed_connector.status == ConnectorStatus.REAUTH_REQUIRED
    assert secret_refresh_token not in (failed_connector.last_error or "")


@requires_infra
def test_an_incremental_scan_keeps_a_trashed_file_and_marks_it_trashed(db: Session) -> None:
    """Deleting the row would erase every action recorded against the file —
    and with it Restore and Undo — while the file still sits in Drive's Trash."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    report = _file("file-q1", "Q1.pdf", parent_id=None)
    drive = _FakeGoogleDriveClient(
        files_pages={None: [DriveFilesPage(files=[report], next_page_token=None)]},
        changes=DriveChangesPage(
            changed_files=[replace(report, trashed=True)],
            removed_file_ids=[],
            next_page_token=None,
            new_start_page_token="start-token-2",
        ),
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )
    service.run(_create_job(db, connector_id=connector.id).id)

    service.run(_create_job(db, connector_id=connector.id, scan_type=ScanType.INCREMENTAL).id)

    db.expire_all()
    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    file = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q1"
    )
    assert file is not None
    assert file.trashed is True


@requires_infra
def test_a_file_deleted_from_the_provider_keeps_its_row_marked_deleted(db: Session) -> None:
    """Removing the row would erase every action recorded against it."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    report = _file("file-q1", "Q1.pdf", parent_id=None)
    drive = _FakeGoogleDriveClient(
        files_pages={None: [DriveFilesPage(files=[report], next_page_token=None)]},
        changes=DriveChangesPage(
            changed_files=[],
            removed_file_ids=["file-q1"],
            next_page_token=None,
            new_start_page_token="start-token-2",
        ),
    )
    service = ScannerService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )
    service.run(_create_job(db, connector_id=connector.id).id)

    service.run(_create_job(db, connector_id=connector.id, scan_type=ScanType.INCREMENTAL).id)

    db.expire_all()
    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    file = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="file-q1"
    )
    assert file is not None
    assert file.permanently_deleted_at is not None
