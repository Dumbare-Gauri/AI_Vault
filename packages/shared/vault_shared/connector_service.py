from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared.connectors.google_workspace import GoogleWorkspaceOAuthClient
from vault_shared.db.models import StorageConnector
from vault_shared.db.repositories import ConnectorCredentialsRepository, StorageConnectorRepository
from vault_shared.errors import NotFoundError, ReauthRequiredError
from vault_shared.security.encryption import decrypt_token, encrypt_token

# Refresh proactively if the stored access token expires within this window
# — avoids handing a caller a token that's valid now but expired by the time
# they actually use it.
_REFRESH_SKEW_SECONDS = 60


class ConnectorTokenService:
    """Shared between apps/backend (the connect/callback/verify/disconnect
    web flow) and apps/worker (Phase 4's scanner) — this is the *only* way
    either app obtains a usable Google access token for a connector. Neither
    app touches `connector_credentials` or `GoogleWorkspaceOAuthClient`
    directly; both go through `get_valid_access_token`.

    Deliberately does not include the interactive OAuth flow itself
    (initiate/complete connect) — that depends on Redis-backed CSRF state
    that's only ever needed by the backend's HTTP presentation layer, so it
    stays there (`apps/backend/app/application/connector_service.py`), which
    composes this class for the token-refresh piece.
    """

    def __init__(self, db: Session, *, oauth_client: GoogleWorkspaceOAuthClient) -> None:
        self._db = db
        self._oauth_client = oauth_client
        self._credentials = ConnectorCredentialsRepository(db)

    def get_valid_access_token(self, connector: StorageConnector) -> str:
        """Refreshes automatically if the stored token is expired or expiring
        soon (Phase 3 spec's "prevent invalid tokens from reaching later
        phases"). Phase 4's scanner calls this — it never touches
        credentials or the OAuth client directly."""
        credentials = self._credentials.get_by_connector_id(connector.id)
        if credentials is None:
            raise NotFoundError("Connector has no stored credentials.")

        if credentials.expires_at <= datetime.now(UTC) + timedelta(seconds=_REFRESH_SKEW_SECONDS):
            refresh_token = decrypt_token(credentials.refresh_token_encrypted)
            try:
                token_set = self._oauth_client.refresh_access_token(refresh_token=refresh_token)
            except ReauthRequiredError as exc:
                # Whatever hit this — a scan, an action, a preview — the
                # connection itself now needs the user to reconnect, so say so.
                StorageConnectorRepository(self._db).mark_reauth_required(connector, error=str(exc))
                self._db.commit()
                raise
            self._credentials.update_access_token(
                credentials,
                access_token_encrypted=encrypt_token(token_set.access_token),
                expires_at=token_set.expires_at,
            )
            self._db.commit()
            return token_set.access_token

        return decrypt_token(credentials.access_token_encrypted)
