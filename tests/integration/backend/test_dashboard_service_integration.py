import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from app.application.auth_service import AuthService
from app.application.dashboard_service import DashboardService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import get_settings
from vault_shared.db.models import ConnectorProvider, DriveType
from vault_shared.db.repositories import (
    FileLifecycleRepository,
    FileRepository,
    OrganizationEntityRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory


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


@requires_infra
def test_get_overview_returns_no_snapshot_for_a_brand_new_organization(db: Session) -> None:
    user = _provision_user(db)
    service = DashboardService(db)

    overview = service.get_overview(user.organization_id)

    assert overview.connectors == []
    assert overview.latest_snapshot is None
    assert overview.snapshot_history == []
    assert overview.latest_scan_status is None


@requires_infra
def test_get_overview_lists_connected_providers(db: Session) -> None:
    user = _provision_user(db)
    _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    service = DashboardService(db)

    overview = service.get_overview(user.organization_id)

    assert len(overview.connectors) == 1


@requires_infra
def test_get_overview_never_leaks_another_organizations_connector(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    _provision_connector(db, organization_id=other_user.organization_id, user_id=other_user.id)
    service = DashboardService(db)

    overview = service.get_overview(user.organization_id)

    assert overview.connectors == []


def _provision_file(db: Session, *, connector_id: uuid.UUID, name: str, trashed: bool = False):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=f"drv-{uuid.uuid4().hex[:12]}",
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=10,
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
    file.trashed = trashed
    db.flush()
    return file


def _score(db: Session, file_id: uuid.UUID, state: str) -> None:
    FileLifecycleRepository(db).upsert(
        file_id=file_id,
        state=state,
        confidence=0.8,
        evidence=["test"],
        signals={},
        scorer_version="test",
        analyzed_at=datetime.now(UTC),
    )


@requires_infra
def test_get_overview_counts_lifecycle_states_and_entities(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _score(db, _provision_file(db, connector_id=connector.id, name="a.txt").id, "archive_candidate")
    _score(db, _provision_file(db, connector_id=connector.id, name="b.txt").id, "archive_candidate")
    _score(db, _provision_file(db, connector_id=connector.id, name="c.txt").id, "review_required")
    OrganizationEntityRepository(db).upsert(
        organization_id=user.organization_id,
        entity_type="project",
        name="Phoenix",
        normalized_name="phoenix",
        confidence=0.9,
        evidence=[],
    )

    intelligence = DashboardService(db).get_overview(user.organization_id).intelligence

    assert intelligence.lifecycle_by_state == {"archive_candidate": 2, "review_required": 1}
    assert intelligence.entities_by_type == {"project": 1}


@requires_infra
def test_get_overview_lifecycle_counts_exclude_trashed_files(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    trashed = _provision_file(db, connector_id=connector.id, name="gone.txt", trashed=True)
    _score(db, trashed.id, "archive_candidate")

    intelligence = DashboardService(db).get_overview(user.organization_id).intelligence

    assert intelligence.lifecycle_by_state == {}
