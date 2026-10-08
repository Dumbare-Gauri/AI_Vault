import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from app.application.auth_service import AuthService
from app.application.file_service import FileService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from vault_shared import NotFoundError, get_settings
from vault_shared.connectors.google_workspace import GoogleTokenSet
from vault_shared.db.models import ConnectorProvider, DriveType, RelationshipType
from vault_shared.db.repositories import (
    FileClassificationRepository,
    FileMetadataRepository,
    FileRelationshipRepository,
    FileRepository,
    FolderRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.storage.default_registry import build_storage_registry


class _FakeGoogleWorkspaceOAuthClient:
    """Stands in for the real OAuth client via constructor injection — none
    of these tests exercise a Drive call, so `refresh_access_token` is
    never actually invoked, but `FileService` requires an `oauth_client` to
    construct its internal `ConnectorTokenService`."""

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        raise NotImplementedError


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


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    return StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )


def _provision_file(
    db: Session,
    *,
    connector_id: uuid.UUID,
    name: str,
    mime_type: str = "application/pdf",
    owner_email: str = "founder@acme.com",
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
        provider_file_id=str(uuid.uuid4()),
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type=mime_type,
        size_bytes=1024,
        owner_email=owner_email,
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=f"https://drive.google.com/file/d/{name}/view",
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    db.commit()
    return file


@requires_infra
def test_list_for_connector_ownership_mine_excludes_shared_with_me_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Mine.pdf", owner_email="founder@acme.com")
    _provision_file(
        db, connector_id=connector.id, name="Shared.pdf", owner_email="someone-else@example.com"
    )
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0, ownership="mine"
    )

    assert total == 1
    assert {f.name for f in files} == {"Mine.pdf"}


@requires_infra
def test_list_for_connector_ownership_shared_excludes_owned_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Mine.pdf", owner_email="founder@acme.com")
    _provision_file(
        db, connector_id=connector.id, name="Shared.pdf", owner_email="someone-else@example.com"
    )
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0, ownership="shared"
    )

    assert total == 1
    assert {f.name for f in files} == {"Shared.pdf"}


@requires_infra
def test_list_for_connector_ownership_none_returns_everything(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Mine.pdf", owner_email="founder@acme.com")
    _provision_file(
        db, connector_id=connector.id, name="Shared.pdf", owner_email="someone-else@example.com"
    )
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0
    )

    assert total == 2
    assert {f.name for f in files} == {"Mine.pdf", "Shared.pdf"}


@requires_infra
def test_list_for_connector_excludes_trashed_files(db: Session) -> None:
    """Regression guard: a file the Execution Engine already trashed
    shouldn't keep showing up in the main Files browse list with
    working-looking Rename/Move/Download buttons."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    active = _provision_file(db, connector_id=connector.id, name="Active.pdf")
    trashed = _provision_file(db, connector_id=connector.id, name="Trashed.pdf")
    FileRepository(db).mark_trashed(trashed, trashed=True)
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0
    )

    assert total == 1
    assert {f.name for f in files} == {"Active.pdf"}
    assert active.name == "Active.pdf"


@requires_infra
def test_list_trashed_for_connector_returns_only_trashed_and_not_yet_permanently_deleted(
    db: Session,
) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    files_repo = FileRepository(db)
    active = _provision_file(db, connector_id=connector.id, name="Active.pdf")
    trashed = _provision_file(db, connector_id=connector.id, name="Trashed.pdf")
    files_repo.mark_trashed(trashed, trashed=True)
    gone = _provision_file(db, connector_id=connector.id, name="Gone.pdf")
    files_repo.mark_trashed(gone, trashed=True)
    files_repo.mark_permanently_deleted(gone)
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_trashed_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0
    )

    assert total == 1
    assert {f.name for f in files} == {"Trashed.pdf"}
    assert active.name == "Active.pdf"


@requires_infra
def test_list_for_connector_returns_files_and_total_count(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Report.pdf")
    _provision_file(db, connector_id=connector.id, name="Invoice.pdf")
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    files, total = service.list_for_connector(
        connector.id, organization_id=user.organization_id, limit=10, offset=0
    )

    assert total == 2
    assert {f.name for f in files} == {"Report.pdf", "Invoice.pdf"}


@requires_infra
def test_list_for_connector_rejects_a_connector_from_another_organization(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    with pytest.raises(NotFoundError):
        service.list_for_connector(
            connector.id, organization_id=other_user.organization_id, limit=10, offset=0
        )


@requires_infra
def test_get_detail_assembles_metadata_classification_and_relationships(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_file(db, connector_id=connector.id, name="Report_v2.pdf")
    related = _provision_file(db, connector_id=connector.id, name="Report_v1.pdf")

    now = datetime.now(UTC)
    FileMetadataRepository(db).upsert(
        file_id=file.id,
        normalized_extension="pdf",
        mime_type_validated=True,
        mime_mismatch_reason=None,
        naming_pattern="versioned",
        version_label="v2",
        owner_summary="founder@acme.com",
        sharing_summary="private",
        language="en",
        enriched_at=now,
    )
    FileClassificationRepository(db).upsert(
        file_id=file.id,
        document_type="Documentation",
        confidence=0.5,
        method="mime_document_default",
        classified_at=now,
    )
    FileRelationshipRepository(db).upsert(
        connector_id=connector.id,
        file_id=file.id,
        related_file_id=related.id,
        relationship_type=RelationshipType.SEQUENTIAL_VERSION,
        confidence=0.85,
        metadata={"match_reason": "explicit_version_sequence"},
        discovered_at=now,
    )
    db.commit()

    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )
    detail = service.get_detail(file.id, organization_id=user.organization_id)

    assert detail.metadata.version_label == "v2"
    assert detail.classification.document_type == "Documentation"
    assert len(detail.related_files) == 1
    assert detail.related_files[0].file.name == "Report_v1.pdf"
    assert (
        detail.related_files[0].relationship.relationship_type
        == RelationshipType.SEQUENTIAL_VERSION
    )


@requires_infra
def test_get_detail_rejects_a_file_from_another_organization(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_file(db, connector_id=connector.id, name="Report.pdf")
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    with pytest.raises(NotFoundError):
        service.get_detail(file.id, organization_id=other_user.organization_id)


@requires_infra
def test_get_detail_handles_a_file_with_no_enrichment_yet(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_file(db, connector_id=connector.id, name="Report.pdf")
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    detail = service.get_detail(file.id, organization_id=user.organization_id)

    assert detail.metadata is None
    assert detail.classification is None
    assert detail.extraction is None
    assert detail.knowledge_attributes == []
    assert detail.related_files == []


@requires_infra
def test_search_folders_matches_by_name_and_excludes_non_folders(db: Session) -> None:
    """Drive folders live in the `folders` table (that's what the scanner
    writes); a file whose name matches is never offered as a destination."""
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    source = StorageSourceRepository(db).upsert(
        connector_id=connector.id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    for provider_id, name in (("fld-fin", "Finance"), ("fld-mkt", "Marketing")):
        FolderRepository(db).upsert(
            storage_source_id=source.id,
            provider_file_id=provider_id,
            provider_parent_id=None,
            parent_folder_id=None,
            name=name,
            path=f"/{name}",
            owner_email="founder@acme.com",
            is_shared=False,
            provider_created_at=None,
            provider_modified_at=None,
            scanned_at=datetime.now(UTC),
        )
    _provision_file(
        db,
        connector_id=connector.id,
        name="Finance.pdf",
        mime_type="application/pdf",
    )
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    folders = service.search_folders(
        connector.id, organization_id=user.organization_id, query="fin", limit=20
    )

    assert [f.name for f in folders] == ["Finance"]


@requires_infra
def test_search_folders_rejects_a_connector_from_another_organization(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    service = FileService(
        db, storage=build_storage_registry(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())
    )

    with pytest.raises(NotFoundError):
        service.search_folders(
            connector.id, organization_id=other_user.organization_id, query="", limit=20
        )
