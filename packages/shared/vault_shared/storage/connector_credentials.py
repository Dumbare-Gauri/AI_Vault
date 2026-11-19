import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from vault_shared.connector_service import ConnectorTokenService
from vault_shared.connectors.google_workspace import GoogleWorkspaceOAuthClient
from vault_shared.db.models import StorageConnector
from vault_shared.db.repositories import ConnectorCredentialsRepository
from vault_shared.logging import get_logger
from vault_shared.security.encryption import TokenEncryptionError, decrypt_token

logger = get_logger("vault_shared.storage.connector_credentials")

# `ConnectorTokenService` hands out a token with at least a minute of life
# left; holding it for half that is always safe and turns "one credential
# lookup + decrypt per file" in a loop into one per half minute.
_TOKEN_CACHE_SECONDS = 30.0


class ConnectorCredentialSource:
    """`CredentialSource` backed by the existing encrypted credential store.

    It composes `ConnectorTokenService` — the one place tokens are decrypted
    and refreshed — rather than duplicating any of it. Instances are bound to
    one connection.
    """

    def __init__(
        self,
        db: Session,
        connector: StorageConnector,
        *,
        oauth_client: GoogleWorkspaceOAuthClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connector = connector
        self._oauth_client = oauth_client
        self._tokens = ConnectorTokenService(db, oauth_client=oauth_client)
        self._credentials = ConnectorCredentialsRepository(db)
        self._clock = clock
        self._cached_token: str | None = None
        self._cached_until = 0.0

    def access_token(self) -> str:
        now = self._clock()
        if self._cached_token is not None and now < self._cached_until:
            return self._cached_token
        token = self._tokens.get_valid_access_token(self._connector)
        self._cached_token = token
        self._cached_until = now + _TOKEN_CACHE_SECONDS
        return token

    def granted_scopes(self) -> tuple[str, ...] | None:
        credentials = self._credentials.get_by_connector_id(self._connector.id)
        if credentials is None:
            return None
        return tuple((credentials.granted_scopes or "").split())

    def revoke(self) -> None:
        credentials = self._credentials.get_by_connector_id(self._connector.id)
        if credentials is None:
            return
        try:
            refresh_token = decrypt_token(credentials.refresh_token_encrypted)
        except TokenEncryptionError:
            logger.warning("storage_disconnect_could_not_decrypt_token_for_revoke")
            return
        self._oauth_client.revoke(token=refresh_token)
        self._cached_token = None
