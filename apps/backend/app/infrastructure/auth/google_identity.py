from dataclasses import dataclass

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token

from vault_shared import UnauthorizedError, get_logger, get_settings

logger = get_logger("app.infrastructure.auth.google_identity")


@dataclass(frozen=True)
class GoogleUserInfo:
    sub: str
    email: str
    email_verified: bool
    name: str
    picture: str | None


class GoogleIdentityVerifier:
    """The only place a Google-specific library is used — mirrors the
    Storage Connector pattern (Engineering Handbook §8.1): provider-specific
    detail stays behind one narrow interface. Verifies a Google Identity
    Services ID token (client-side sign-in) against Google's public keys;
    never trusts claims without verifying the signature first."""

    def __init__(self, *, client_id: str) -> None:
        self._client_id = client_id

    def verify(self, id_token_jwt: str) -> GoogleUserInfo:
        try:
            claims = google_id_token.verify_oauth2_token(
                id_token_jwt, google_auth_requests.Request(), audience=self._client_id
            )
        except (GoogleAuthError, ValueError) as exc:
            logger.warning("google_id_token_verification_failed", extra={"error": str(exc)})
            raise UnauthorizedError(
                "Google sign-in failed — invalid or expired credential."
            ) from exc

        try:
            user = GoogleUserInfo(
                sub=claims["sub"],
                email=claims["email"],
                email_verified=_is_verified(claims.get("email_verified")),
                name=claims.get("name", claims["email"]),
                picture=claims.get("picture"),
            )
        except KeyError as exc:
            raise UnauthorizedError(
                "Google sign-in failed — incomplete profile information."
            ) from exc
        if not user.email_verified:
            # An unverified address isn't proof of who owns it; the account
            # is never created or signed into on its strength.
            logger.warning("google_sign_in_unverified_email")
            raise UnauthorizedError(
                "Google sign-in failed — verify this account's email address with Google first."
            )
        return user


def _is_verified(claim: object) -> bool:
    # Google sends a boolean, or the string "true" in some older tokens;
    # nothing else (not even 1, which equals True in Python) counts.
    return claim is True or claim == "true"


def get_google_identity_verifier() -> GoogleIdentityVerifier:
    """FastAPI dependency provider — overridden in tests with a fake verifier
    so the login flow is testable without a real Google ID token."""
    return GoogleIdentityVerifier(client_id=get_settings().google_client_id)
