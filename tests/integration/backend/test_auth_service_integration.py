import socket
import uuid
from urllib.parse import urlparse

import pytest
from app.application.auth_service import AuthService
from app.application.organization_service import OrganizationService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import ConflictError, UnauthorizedError, get_settings
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
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI service containers.",
)


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        # Rolls back anything left uncommitted — service methods themselves
        # commit explicitly, so this doesn't erase rows created by the test.
        # Each test uses a unique-per-run google_sub/email (see _google_user)
        # specifically so re-running this suite against a persistent dev
        # database doesn't collide on unique constraints.
        session.rollback()
        session.close()


def _google_user(*, sub: str | None = None, email: str | None = None, name: str = "Ada Founder") -> GoogleUserInfo:
    unique = uuid.uuid4().hex[:12]
    return GoogleUserInfo(
        sub=sub or f"google-sub-{unique}",
        email=email or f"founder-{unique}@example.com",
        email_verified=True,
        name=name,
        picture=None,
    )


@requires_infra
def test_first_login_provisions_an_organization_and_user_as_owner(db: Session) -> None:
    service = AuthService(db)

    session = service.complete_google_login(google_user=_google_user(), ip_address="127.0.0.1")

    assert session.user.role.name == "owner"
    assert session.user.organization.name.endswith("Organization")
    assert session.access_token
    assert session.refresh_token


@requires_infra
def test_second_login_with_the_same_google_sub_reuses_the_existing_user(db: Session) -> None:
    service = AuthService(db)
    google_user = _google_user()

    first = service.complete_google_login(google_user=google_user, ip_address=None)
    second = service.complete_google_login(google_user=google_user, ip_address=None)

    assert first.user.id == second.user.id
    assert second.user.last_login_at is not None


@requires_infra
def test_login_with_an_existing_email_but_different_sub_is_a_conflict(db: Session) -> None:
    service = AuthService(db)
    unique = uuid.uuid4().hex[:12]
    email = f"dup-{unique}@example.com"
    service.complete_google_login(
        google_user=_google_user(sub=f"sub-a-{unique}", email=email), ip_address=None
    )

    with pytest.raises(ConflictError):
        service.complete_google_login(
            google_user=_google_user(sub=f"sub-b-{unique}", email=email), ip_address=None
        )


@requires_infra
def test_refresh_rotates_the_token_and_invalidates_the_old_one(db: Session) -> None:
    service = AuthService(db)
    session = service.complete_google_login(google_user=_google_user(), ip_address=None)

    refreshed = service.refresh_session(raw_refresh_token=session.refresh_token, ip_address=None)

    assert refreshed.refresh_token != session.refresh_token
    with pytest.raises(UnauthorizedError):
        service.refresh_session(raw_refresh_token=session.refresh_token, ip_address=None)


@requires_infra
def test_reusing_a_revoked_refresh_token_revokes_the_whole_family(db: Session) -> None:
    service = AuthService(db)
    session = service.complete_google_login(google_user=_google_user(), ip_address=None)
    refreshed = service.refresh_session(raw_refresh_token=session.refresh_token, ip_address=None)

    # Reusing the original (now-revoked) token fails...
    with pytest.raises(UnauthorizedError):
        service.refresh_session(raw_refresh_token=session.refresh_token, ip_address=None)

    # ...and revokes the rotated token too (the whole family), not just the
    # one that was reused.
    with pytest.raises(UnauthorizedError):
        service.refresh_session(raw_refresh_token=refreshed.refresh_token, ip_address=None)


@requires_infra
def test_logout_revokes_the_refresh_token(db: Session) -> None:
    service = AuthService(db)
    session = service.complete_google_login(google_user=_google_user(), ip_address=None)

    service.logout(raw_refresh_token=session.refresh_token, ip_address=None)

    with pytest.raises(UnauthorizedError):
        service.refresh_session(raw_refresh_token=session.refresh_token, ip_address=None)


@requires_infra
def test_organization_service_renames_the_organization(db: Session) -> None:
    auth_service = AuthService(db)
    session = auth_service.complete_google_login(google_user=_google_user(), ip_address=None)

    organization_service = OrganizationService(db)
    renamed = organization_service.rename(session.user.organization, name="Renamed Co")

    assert renamed.name == "Renamed Co"


@requires_infra
def test_an_unverified_email_never_creates_or_enters_an_account(db: Session) -> None:
    from dataclasses import replace

    from vault_shared.db.repositories import UserRepository

    unverified = replace(_google_user(), email_verified=False)

    with pytest.raises(UnauthorizedError):
        AuthService(db).complete_google_login(google_user=unverified, ip_address=None)

    assert UserRepository(db).get_by_google_sub(unverified.sub) is None
