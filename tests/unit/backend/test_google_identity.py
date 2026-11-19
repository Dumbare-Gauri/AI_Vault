from unittest.mock import patch

import pytest
from app.infrastructure.auth.google_identity import GoogleIdentityVerifier
from vault_shared import UnauthorizedError


def test_verify_returns_user_info_on_a_valid_token() -> None:
    claims = {
        "sub": "1234567890",
        "email": "founder@example.com",
        "email_verified": True,
        "name": "Ada Founder",
        "picture": "https://example.com/avatar.png",
    }

    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        return_value=claims,
    ):
        verifier = GoogleIdentityVerifier(client_id="test-client-id")
        result = verifier.verify("some-id-token")

    assert result.sub == "1234567890"
    assert result.email == "founder@example.com"
    assert result.email_verified is True
    assert result.name == "Ada Founder"
    assert result.picture == "https://example.com/avatar.png"


def test_verify_raises_unauthorized_when_google_rejects_the_token() -> None:
    from google.auth.exceptions import GoogleAuthError

    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        side_effect=GoogleAuthError("token expired"),
    ):
        verifier = GoogleIdentityVerifier(client_id="test-client-id")
        with pytest.raises(UnauthorizedError):
            verifier.verify("expired-token")


def test_verify_raises_unauthorized_on_incomplete_claims() -> None:
    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        return_value={"sub": "123"},  # missing email
    ):
        verifier = GoogleIdentityVerifier(client_id="test-client-id")
        with pytest.raises(UnauthorizedError):
            verifier.verify("some-id-token")


def test_verify_defaults_optional_fields_when_omitted() -> None:
    claims = {"sub": "999", "email": "person@example.com", "email_verified": True}

    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        return_value=claims,
    ):
        verifier = GoogleIdentityVerifier(client_id="test-client-id")
        result = verifier.verify("some-id-token")

    assert result.name == "person@example.com"
    assert result.picture is None


@pytest.mark.parametrize("verified", [False, "false", None, "yes", 1])
def test_verify_rejects_an_email_google_has_not_verified(verified: object) -> None:
    claims = {"sub": "999", "email": "person@example.com"}
    if verified is not None:
        claims["email_verified"] = verified

    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        return_value=claims,
    ):
        verifier = GoogleIdentityVerifier(client_id="test-client-id")
        with pytest.raises(UnauthorizedError, match="verify"):
            verifier.verify("some-id-token")


def test_verify_accepts_googles_string_form_of_a_verified_email() -> None:
    claims = {"sub": "999", "email": "person@example.com", "email_verified": "true"}

    with patch(
        "app.infrastructure.auth.google_identity.google_id_token.verify_oauth2_token",
        return_value=claims,
    ):
        result = GoogleIdentityVerifier(client_id="test-client-id").verify("some-id-token")

    assert result.email_verified is True
