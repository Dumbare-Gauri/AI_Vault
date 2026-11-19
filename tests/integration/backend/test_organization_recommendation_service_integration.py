import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from app.application import organization_recommendation_service as service_module
from app.application.organization_recommendation_service import (
    OrganizationRecommendationService,
)
from vault_shared import ConflictError, NotFoundError, ValidationError, get_settings
from vault_shared.db.models import ConnectorProvider, DriveType, RoleName
from vault_shared.db.repositories import (
    ConnectorCredentialsRepository,
    FileRepository,
    OrganizationEntityRepository,
    OrganizationRecommendationRepository,
    OrganizationRepository,
    RoleRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
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


requires_infra = pytest.mark.skipif(
    not (_reachable(get_settings().database_url) and _reachable(get_settings().redis_url)),
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


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        service_module,
        "enqueue_organization_recommendation_apply",
        lambda recommendation_id, **_kwargs: calls.append(recommendation_id),
    )
    return calls


def _provision(db: Session, *, with_write_credentials: bool):
    unique = uuid.uuid4().hex[:12]
    organization = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
    user = UserRepository(db).create(
        organization_id=organization.id,
        role_id=RoleRepository(db).get_by_name(RoleName.OWNER).id,
        google_sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        name="Ada Founder",
        avatar_url=None,
    )
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=organization.id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user.id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )
    if with_write_credentials:
        ConnectorCredentialsRepository(db).upsert(
            connector_id=connector.id,
            access_token_encrypted=encrypt_token("access-1"),
            refresh_token_encrypted=encrypt_token("refresh-1"),
            granted_scopes=DRIVE_WRITE_SCOPE,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    source = StorageSourceRepository(db).upsert(
        connector_id=connector.id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=f"drv-{unique}",
        provider_parent_id=None,
        parent_folder_id=None,
        name="Brief.docx",
        path="/Brief.docx",
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
    entity = OrganizationEntityRepository(db).upsert(
        organization_id=organization.id,
        entity_type="project",
        name="Acme Rebrand",
        normalized_name="acme rebrand",
        confidence=0.9,
        evidence=[],
    )
    recommendation = OrganizationRecommendationRepository(db).upsert_for_entity(
        organization_id=organization.id,
        entity_id=entity.id,
        kind="group_project",
        title="Consolidate",
        reasoning_summary="Scattered.",
        evidence=[],
        confidence=0.9,
        affected_file_ids=[str(file.id)],
        current_locations=[],
        suggested_destination=["Projects", "Acme Rebrand"],
        estimated_storage_impact_bytes=10,
    )
    db.commit()
    return user, recommendation


@requires_infra
def test_apply_is_refused_immediately_when_the_connection_cannot_write(
    db: Session, enqueued: list[uuid.UUID]
) -> None:
    user, recommendation = _provision(db, with_write_credentials=False)

    with pytest.raises(ValidationError, match="no stored credentials"):
        OrganizationRecommendationService(db).trigger_apply(
            recommendation.id, organization_id=user.organization_id, user_id=user.id
        )

    assert enqueued == []


@requires_infra
def test_apply_is_dispatched_when_the_connection_can_write(
    db: Session, enqueued: list[uuid.UUID]
) -> None:
    user, recommendation = _provision(db, with_write_credentials=True)

    OrganizationRecommendationService(db).trigger_apply(
        recommendation.id, organization_id=user.organization_id, user_id=user.id
    )

    assert enqueued == [recommendation.id]


@requires_infra
def test_an_already_applied_recommendation_cannot_be_applied_again(
    db: Session, enqueued: list[uuid.UUID]
) -> None:
    user, recommendation = _provision(db, with_write_credentials=True)
    recommendation.status = "applied"
    db.commit()

    with pytest.raises(ConflictError):
        OrganizationRecommendationService(db).trigger_apply(
            recommendation.id, organization_id=user.organization_id, user_id=user.id
        )

    assert enqueued == []


@requires_infra
def test_another_organization_cannot_apply_the_recommendation(
    db: Session, enqueued: list[uuid.UUID]
) -> None:
    _owner, recommendation = _provision(db, with_write_credentials=True)
    intruder, _ = _provision(db, with_write_credentials=True)

    with pytest.raises(NotFoundError):
        OrganizationRecommendationService(db).trigger_apply(
            recommendation.id, organization_id=intruder.organization_id, user_id=intruder.id
        )

    assert enqueued == []
