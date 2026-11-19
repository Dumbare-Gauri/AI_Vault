import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests

from vault_shared.errors import DependencyUnavailableError, ReauthRequiredError, UnauthorizedError
from vault_shared.logging import get_logger
from vault_shared.settings import get_settings

logger = get_logger("vault_shared.connectors.google_workspace")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# The scope the Execution Engine's mutating calls require (ADR-020) — a
# connector authorized before Phase 8 only has `drive.readonly`, which cannot
# move/rename/trash/update a file.
DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"

_REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class GoogleTokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    granted_scopes: str


@dataclass(frozen=True)
class GoogleAccountInfo:
    email: str
    workspace_domain: str | None


class GoogleWorkspaceOAuthClient:
    """The only place Google Workspace's OAuth endpoints are called — mirrors
    the Storage Connector pattern (Engineering Handbook §8.1): provider
    detail stays behind this one interface, never leaking into
    ConnectorService. A distinct, server-side flow from Phase 2's client-side
    Google Identity Services login — see ADR-014."""

    def __init__(
        self, *, client_id: str, client_secret: str, redirect_uri: str, scopes: str
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes

    def build_authorize_url(self, *, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scopes,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> GoogleTokenSet:
        response = self._post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
            },
        )
        payload = self._extract_json(response, context="token exchange")
        return self._to_token_set(payload, refresh_token=payload.get("refresh_token"))

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        response = self._post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        payload = self._extract_json(response, context="token refresh")
        # Google doesn't reissue a refresh token on a refresh-grant call —
        # the caller keeps using the one it already has.
        return self._to_token_set(payload, refresh_token=refresh_token)

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        try:
            response = requests.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise DependencyUnavailableError(
                "Could not reach Google's Workspace service."
            ) from exc

        payload = self._extract_json(response, context="account info")
        try:
            email = payload["email"]
        except KeyError as exc:
            raise UnauthorizedError("Google did not return an account email.") from exc
        return GoogleAccountInfo(email=email, workspace_domain=payload.get("hd"))

    def revoke(self, *, token: str) -> bool:
        """Best-effort — a failed revoke should never block disconnect from
        completing locally; the credentials row is deleted either way."""
        try:
            response = requests.post(
                GOOGLE_REVOKE_URL, data={"token": token}, timeout=_REQUEST_TIMEOUT_SECONDS
            )
            return response.status_code == 200
        except requests.RequestException:
            logger.warning("google_workspace_revoke_failed")
            return False

    def _post(self, url: str, *, data: dict) -> requests.Response:
        try:
            return requests.post(url, data=data, timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise DependencyUnavailableError(
                "Could not reach Google's Workspace service."
            ) from exc

    @staticmethod
    def _extract_json(response: requests.Response, *, context: str) -> dict:
        if response.status_code != 200:
            # Google's OAuth error body is a static, non-secret shape —
            # {"error": "invalid_grant", "error_description": "..."} — never
            # the token itself, so both fields are safe to log (Handbook
            # §13's "token state, error category" allowance) and are what
            # actually distinguishes a dead refresh token from every other
            # failure mode instead of guessing from the HTTP status alone.
            error_code: str | None = None
            with contextlib.suppress(ValueError):
                error_code = response.json().get("error")
            logger.warning(
                "google_workspace_request_failed",
                extra={
                    "context": context,
                    "status_code": response.status_code,
                    "error_code": error_code,
                },
            )
            if error_code == "invalid_grant":
                raise ReauthRequiredError(
                    "Google authorization has expired or was revoked. Reconnect to continue."
                )
            raise UnauthorizedError(f"Google Workspace {context} failed.")
        return response.json()

    @staticmethod
    def _to_token_set(payload: dict, *, refresh_token: str | None) -> GoogleTokenSet:
        # A 200 response is not a guarantee the body is well-formed — treat a
        # missing `access_token` the same as any other auth failure (a typed
        # VaultError) rather than letting a raw KeyError escape as an
        # unhandled 500 from the presentation layer.
        try:
            access_token = payload["access_token"]
        except KeyError as exc:
            raise UnauthorizedError("Google Workspace returned a malformed response.") from exc
        expires_in = int(payload.get("expires_in", 3600))
        return GoogleTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            granted_scopes=payload.get("scope", ""),
        )


def get_google_workspace_oauth_client() -> GoogleWorkspaceOAuthClient:
    """FastAPI dependency provider — overridden in tests with a fake client
    so the connector flow is testable without real Google credentials."""
    settings = get_settings()
    return GoogleWorkspaceOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_workspace_redirect_uri,
        scopes=settings.google_workspace_scopes,
    )
