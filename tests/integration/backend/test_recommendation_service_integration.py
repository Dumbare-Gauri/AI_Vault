import socket
import uuid
from urllib.parse import urlparse

import pytest
from workspace_testing import workspace_of
from app.application.auth_service import AuthService
from app.application.recommendation_service import RecommendationService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import ConflictError, ForbiddenError, NotFoundError, get_settings
from vault_shared.db.models import RecommendationCategory, RecommendationJobStatus
from vault_shared.db.repositories import (
    RecommendationJobRepository,
    RecommendationRepository,
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


def _provision_recommendation(
    db: Session, *, organization_id: uuid.UUID, rule_name: str, category: str
):
    job = RecommendationJobRepository(db).create(
        organization_id=organization_id, triggered_by="manual", triggered_by_user_id=None
    )
    recommendation = RecommendationRepository(db).upsert(
        organization_id=organization_id, connector_id=workspace_of(db, organization_id).id,
        recommendation_job_id=job.id,
        rule_name=rule_name,
        category=category,
        title=f"{rule_name} fired",
        description="A test recommendation.",
        confidence=0.8,
        estimated_impact="some impact",
        impact_value=10.0,
        risk_level="low",
        suggested_action="Do something.",
        related_departments=[],
        affected_file_ids=[],
        priority_score=50.0,
    )
    db.commit()
    return recommendation


@requires_infra
def test_member_never_sees_security_recommendations(db: Session) -> None:
    user = _provision_user(db)
    _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="orphaned_ownership",
        category=RecommendationCategory.SECURITY,
    )
    _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="duplicate_files",
        category=RecommendationCategory.STORAGE_OPTIMIZATION,
    )
    service = RecommendationService(db)

    member_results = service.list_for_organization(user.organization_id, role="member")
    owner_results = service.list_for_organization(user.organization_id, role="owner")

    assert {r.category for r in member_results} == {RecommendationCategory.STORAGE_OPTIMIZATION}
    assert {r.category for r in owner_results} == {
        RecommendationCategory.SECURITY,
        RecommendationCategory.STORAGE_OPTIMIZATION,
    }


@requires_infra
def test_get_owned_forbids_a_member_from_viewing_a_security_recommendation(db: Session) -> None:
    user = _provision_user(db)
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="orphaned_ownership",
        category=RecommendationCategory.SECURITY,
    )
    service = RecommendationService(db)

    with pytest.raises(ForbiddenError):
        service.get_owned(
            recommendation.id, organization_id=user.organization_id, user_id=user.id, role="member"
        )


@requires_infra
def test_get_owned_allows_an_owner_to_view_a_security_recommendation(db: Session) -> None:
    user = _provision_user(db)
    recommendation = _provision_recommendation(
        db,
        organization_id=user.organization_id,
        rule_name="orphaned_ownership",
        category=RecommendationCategory.SECURITY,
    )
    service = RecommendationService(db)

    result = service.get_owned(
        recommendation.id, organization_id=user.organization_id, user_id=user.id, role="owner"
    )

    assert result.id == recommendation.id


@requires_infra
def test_get_owned_rejects_a_recommendation_from_another_organization(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    recommendation = _provision_recommendation(
        db,
        organization_id=other_user.organization_id,
        rule_name="duplicate_files",
        category=RecommendationCategory.STORAGE_OPTIMIZATION,
    )
    service = RecommendationService(db)

    with pytest.raises(NotFoundError):
        service.get_owned(
            recommendation.id, organization_id=user.organization_id, user_id=user.id, role="owner"
        )


@requires_infra
def test_trigger_refresh_creates_a_pending_job(db: Session) -> None:
    user = _provision_user(db)
    service = RecommendationService(db)

    job = service.trigger_refresh(organization_id=user.organization_id, user_id=user.id)

    assert job.status == RecommendationJobStatus.PENDING
    assert job.organization_id == user.organization_id


@requires_infra
def test_trigger_refresh_rejects_a_second_run_while_one_is_active(db: Session) -> None:
    user = _provision_user(db)
    service = RecommendationService(db)
    service.trigger_refresh(organization_id=user.organization_id, user_id=user.id)

    with pytest.raises(ConflictError):
        service.trigger_refresh(organization_id=user.organization_id, user_id=user.id)
