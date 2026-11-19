import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, get_settings
from vault_shared.connectors.google_workspace import GoogleAccountInfo, GoogleTokenSet
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    EnrichmentJobStatus,
    EnrichmentTrigger,
    RoleName,
)
from vault_shared.db.repositories import (
    ConnectorCredentialsRepository,
    EnrichmentJobRepository,
    FileClassificationRepository,
    FileExtractionRepository,
    FileMetadataRepository,
    FileRelationshipRepository,
    FileRepository,
    OrganizationRepository,
    RoleRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import encrypt_token
from vault_shared.storage.default_registry import build_storage_registry
from worker.enrichment.enrichment_service import EnrichmentService


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
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        )

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        return GoogleAccountInfo(email="founder@acme.com", workspace_domain="acme.com")

    def build_authorize_url(self, *, state: str) -> str:
        raise NotImplementedError

    def revoke(self, *, token: str) -> bool:
        return True


class _FakeGoogleDriveClient:
    """Canned content, keyed by provider_file_id — never calls the real
    Google Drive API. `raise_for_file_id` simulates an unexpected, non-
    network failure for one specific file (a corrupt-content bug, not a
    connectivity problem) to prove per-file failure isolation."""

    def __init__(
        self,
        *,
        content_by_file_id: dict[str, bytes] | None = None,
        raise_for_file_id: str | None = None,
        raise_dependency_unavailable: bool = False,
    ) -> None:
        self._content_by_file_id = content_by_file_id or {}
        self._raise_for_file_id = raise_for_file_id
        self._raise_dependency_unavailable = raise_dependency_unavailable
        self.download_calls = 0

    def download_file(self, *, access_token: str, file_id: str) -> bytes:
        self.download_calls += 1
        if self._raise_dependency_unavailable:
            raise DependencyUnavailableError("Google Drive rate limit exceeded.")
        if file_id == self._raise_for_file_id:
            raise ValueError("simulated unexpected extraction bug")
        return self._content_by_file_id.get(file_id, b"")

    def export_file(self, *, access_token: str, file_id: str, export_mime_type: str) -> bytes:
        raise NotImplementedError

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
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("access-1"),
        refresh_token_encrypted=encrypt_token("refresh-1"),
        granted_scopes="https://www.googleapis.com/auth/drive.readonly",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return connector


def _provision_file(
    db: Session, *, connector_id: uuid.UUID, name: str, mime_type: str, provider_file_id: str
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
        mime_type=mime_type,
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


def _create_job(db: Session, *, connector_id: uuid.UUID):
    job = EnrichmentJobRepository(db).create(
        connector_id=connector_id, triggered_by=EnrichmentTrigger.MANUAL, triggered_by_user_id=None
    )
    db.commit()
    return job


@requires_infra
def test_enrichment_processes_pending_files_and_persists_metadata_and_classification(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_file(
        db,
        connector_id=connector.id,
        name="Client_Invoice.txt",
        mime_type="text/plain",
        provider_file_id="f-invoice",
    )
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(content_by_file_id={"f-invoice": b"invoice text"})
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    completed_job = EnrichmentJobRepository(db).get_by_id(job.id)
    assert completed_job.status == EnrichmentJobStatus.COMPLETED

    metadata = FileMetadataRepository(db).get_by_file_id(file.id)
    assert metadata is not None
    assert metadata.normalized_extension == "txt"

    classification = FileClassificationRepository(db).get_by_file_id(file.id)
    assert classification.document_type == "Invoice"

    extraction = FileExtractionRepository(db).get_by_file_id(file.id)
    assert extraction.status == "success"
    assert extraction.extracted_text == "invoice text"


@requires_infra
def test_enrichment_is_resumable_and_skips_already_enriched_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db,
        connector_id=connector.id,
        name="Report.txt",
        mime_type="text/plain",
        provider_file_id="f-1",
    )
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(content_by_file_id={"f-1": b"report text"})
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )
    service.run(job.id)
    assert drive.download_calls == 1

    second_job = _create_job(db, connector_id=connector.id)
    service.run(second_job.id)

    # Nothing changed since the first run, so the second run finds no
    # pending files and never re-downloads content — this is what makes
    # re-triggering enrichment safe/cheap when nothing actually changed.
    assert drive.download_calls == 1
    progress = EnrichmentJobRepository(db).get_by_id(second_job.id)
    assert progress.status == EnrichmentJobStatus.COMPLETED


@requires_infra
def test_enrichment_discovers_relationships_across_files_in_the_same_job(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db,
        connector_id=connector.id,
        name="Report_v1.txt",
        mime_type="text/plain",
        provider_file_id="f-v1",
    )
    _provision_file(
        db,
        connector_id=connector.id,
        name="Report_v2.txt",
        mime_type="text/plain",
        provider_file_id="f-v2",
    )
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(content_by_file_id={"f-v1": b"v1", "f-v2": b"v2"})
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    source = StorageSourceRepository(db).get_by_connector_and_provider_drive_id(
        connector_id=connector.id, provider_drive_id="root"
    )
    v1 = FileRepository(db).get_by_source_and_provider_id(
        storage_source_id=source.id, provider_file_id="f-v1"
    )
    relationships = FileRelationshipRepository(db).list_for_file(v1.id)
    assert any(r.relationship_type == "sequential_version" for r in relationships)


@requires_infra
def test_a_single_files_unexpected_failure_does_not_stop_the_job(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    good_file = _provision_file(
        db,
        connector_id=connector.id,
        name="Good.txt",
        mime_type="text/plain",
        provider_file_id="f-good",
    )
    _provision_file(
        db,
        connector_id=connector.id,
        name="Bad.txt",
        mime_type="text/plain",
        provider_file_id="f-bad",
    )
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(
        content_by_file_id={"f-good": b"good text"}, raise_for_file_id="f-bad"
    )
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    completed_job = EnrichmentJobRepository(db).get_by_id(job.id)
    assert completed_job.status == EnrichmentJobStatus.COMPLETED

    good_metadata = FileMetadataRepository(db).get_by_file_id(good_file.id)
    assert good_metadata is not None


@requires_infra
def test_dependency_unavailable_leaves_the_job_running_for_a_celery_level_retry(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db,
        connector_id=connector.id,
        name="Report.txt",
        mime_type="text/plain",
        provider_file_id="f-1",
    )
    job = _create_job(db, connector_id=connector.id)

    drive = _FakeGoogleDriveClient(raise_dependency_unavailable=True)
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    with pytest.raises(DependencyUnavailableError):
        service.run(job.id)

    still_running = EnrichmentJobRepository(db).get_by_id(job.id)
    assert still_running.status == EnrichmentJobStatus.RUNNING


@requires_infra
def test_cancellation_stops_the_job_cleanly(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(
        db,
        connector_id=connector.id,
        name="Report.txt",
        mime_type="text/plain",
        provider_file_id="f-1",
    )
    job = _create_job(db, connector_id=connector.id)
    jobs = EnrichmentJobRepository(db)
    jobs.request_cancel(job)
    db.commit()

    drive = _FakeGoogleDriveClient(content_by_file_id={"f-1": b"text"})
    service = EnrichmentService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
    )

    service.run(job.id)

    cancelled_job = jobs.get_by_id(job.id)
    assert cancelled_job.status == EnrichmentJobStatus.CANCELLED
