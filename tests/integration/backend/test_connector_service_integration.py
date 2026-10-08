import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from app.application.auth_service import AuthService
from app.application.connector_service import ConnectorService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from sqlalchemy.orm import Session
from vault_shared import (
    ConflictError,
    ReauthRequiredError,
    UnauthorizedError,
    get_settings,
)
from vault_shared.connectors.google_workspace import (
    GoogleAccountInfo,
    GoogleTokenSet,
)
from vault_shared.connector_service import ConnectorTokenService
from vault_shared.db.models import ConnectorStatus
from vault_shared.db.repositories import ConnectorCredentialsRepository, StorageConnectorRepository
from vault_shared.db.session import get_session_factory
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
    """Exercises the real ConnectorService + real Postgres, without any real
    network call to Google — the fake stands in exactly where
    GoogleWorkspaceOAuthClient would via constructor injection."""

    def __init__(self, *, initial_refresh_token: str = "refresh-1") -> None:
        self.refresh_calls = 0
        self.revoked_tokens: list[str] = []
        self._refresh_token = initial_refresh_token

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, *, code: str) -> GoogleTokenSet:
        return GoogleTokenSet(
            access_token="access-1",
            refresh_token=self._refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="openid email https://www.googleapis.com/auth/drive.readonly",
        )

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        self.refresh_calls += 1
        return GoogleTokenSet(
            access_token=f"access-refreshed-{self.refresh_calls}",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="openid email https://www.googleapis.com/auth/drive.readonly",
        )

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        return GoogleAccountInfo(email="founder@acme.com", workspace_domain="acme.com")

    def revoke(self, *, token: str) -> bool:
        self.revoked_tokens.append(token)
        return True


def _provision_user(db: Session):
    """Reuses Phase 2's AuthService to get a real Organization+User+Role
    trio, exactly as a real sign-in would produce."""
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


@requires_infra
def test_initiate_connect_returns_an_authorize_url_and_records_an_audit_event(db: Session) -> None:
    user = _provision_user(db)
    service = ConnectorService(db, oauth_client=_FakeGoogleWorkspaceOAuthClient())

    url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)

    assert url.startswith("https://accounts.google.com")


@requires_infra
def test_complete_connect_persists_a_connected_connector_with_encrypted_credentials(
    db: Session,
) -> None:
    user = _provision_user(db)
    oauth_client = _FakeGoogleWorkspaceOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)

    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]

    connector = service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    assert connector.status == ConnectorStatus.CONNECTED
    assert connector.account_email == "founder@acme.com"
    assert connector.workspace_domain == "acme.com"

    # The raw token never appears anywhere queryable except via the service's
    # own decrypt path — confirmed by getting a usable token back out.
    access_token = service.get_valid_access_token(connector)
    assert access_token == "access-1"


@requires_infra
def test_complete_connect_rejects_a_state_issued_for_a_different_organization(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    oauth_client = _FakeGoogleWorkspaceOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)

    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]

    with pytest.raises(UnauthorizedError):
        service.complete_connect(
            code="auth-code",
            state=state,
            organization_id=other_user.organization_id,
            user_id=other_user.id,
            ip_address=None,
        )


@requires_infra
def test_initiate_connect_rejects_a_duplicate_attempt_while_already_connected(db: Session) -> None:
    user = _provision_user(db)
    oauth_client = _FakeGoogleWorkspaceOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)

    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]
    service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    with pytest.raises(ConflictError):
        service.initiate_connect(organization_id=user.organization_id, user_id=user.id)


@requires_infra
def test_get_valid_access_token_refreshes_when_expired(db: Session) -> None:
    user = _provision_user(db)
    oauth_client = _FakeGoogleWorkspaceOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)
    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]
    connector = service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    # Force the stored token to look already-expired.
    credentials = service._credentials.get_by_connector_id(connector.id)
    credentials.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    access_token = service.get_valid_access_token(connector)

    assert access_token == "access-refreshed-1"
    assert oauth_client.refresh_calls == 1


@requires_infra
def test_verify_marks_the_connector_error_when_the_provider_call_fails(db: Session) -> None:
    class _FailingOAuthClient(_FakeGoogleWorkspaceOAuthClient):
        """Succeeds on the first call (used by `complete_connect` to set up
        a connected connector) and fails on every call after — otherwise
        `complete_connect` itself would fail before `verify` is ever
        reached, since both call `fetch_account_info`."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
            self.calls += 1
            if self.calls == 1:
                return super().fetch_account_info(access_token=access_token)
            raise UnauthorizedError("Google Workspace account info failed.")

    user = _provision_user(db)
    oauth_client = _FailingOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)
    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]
    connector = service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    verified = service.verify(connector, ip_address=None)

    assert verified.status == ConnectorStatus.ERROR
    assert verified.last_error is not None


@requires_infra
def test_verify_marks_the_connector_reauth_required_on_a_revoked_refresh_token(db: Session) -> None:
    """The exact real-world blocker this test guards against: a scan (or a
    manual Verify) discovering the stored refresh token has been revoked
    must flip the connector to REAUTH_REQUIRED — not the generic ERROR —
    so the UI can offer a one-click reconnect instead of a raw error."""

    class _RevokedOAuthClient(_FakeGoogleWorkspaceOAuthClient):
        def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
            raise ReauthRequiredError("Google authorization has expired or was revoked.")

    user = _provision_user(db)
    oauth_client = _RevokedOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)
    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]
    connector = service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    # Force the stored token to look already-expired so `verify` takes the
    # refresh path (where the revoked-token failure actually surfaces).
    credentials = service._credentials.get_by_connector_id(connector.id)
    credentials.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    verified = service.verify(connector, ip_address=None)

    assert verified.status == ConnectorStatus.REAUTH_REQUIRED
    assert verified.last_error is not None


@requires_infra
def test_disconnect_deletes_credentials_and_revokes_the_token(db: Session) -> None:
    user = _provision_user(db)
    oauth_client = _FakeGoogleWorkspaceOAuthClient()
    service = ConnectorService(db, oauth_client=oauth_client)
    authorize_url = service.initiate_connect(organization_id=user.organization_id, user_id=user.id)
    state = authorize_url.rsplit("state=", 1)[1]
    connector = service.complete_connect(
        code="auth-code", state=state, organization_id=user.organization_id, user_id=user.id, ip_address=None
    )

    disconnected = service.disconnect(connector, ip_address=None)

    assert disconnected.status == ConnectorStatus.DISCONNECTED
    assert oauth_client.revoked_tokens == ["refresh-1"]
    assert service._credentials.get_by_connector_id(connector.id) is None


class _RevokedOAuthClient(_FakeGoogleWorkspaceOAuthClient):
    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        raise ReauthRequiredError("Google authorization has expired or was revoked.")


@requires_infra
def test_a_refused_token_refresh_marks_the_connection_for_reconnect(db: Session) -> None:
    user = _provision_user(db)
    connector = StorageConnectorRepository(db).upsert_connected(
        organization_id=user.organization_id,
        provider="google_workspace",
        connected_by_user_id=user.id,
        account_email="founder@acme.com",
        workspace_domain=None,
    )
    ConnectorCredentialsRepository(db).upsert(
        connector_id=connector.id,
        access_token_encrypted=encrypt_token("stale"),
        refresh_token_encrypted=encrypt_token("revoked"),
        granted_scopes="https://www.googleapis.com/auth/drive",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.commit()

    with pytest.raises(ReauthRequiredError):
        ConnectorTokenService(db, oauth_client=_RevokedOAuthClient()).get_valid_access_token(
            connector
        )

    db.expire_all()
    assert StorageConnectorRepository(db).get_by_id(connector.id).status == (
        ConnectorStatus.REAUTH_REQUIRED
    )
