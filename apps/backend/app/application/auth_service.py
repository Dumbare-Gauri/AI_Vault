import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.infrastructure.auth.google_identity import GoogleUserInfo
from app.infrastructure.auth.jwt import AccessTokenClaims, create_access_token
from app.infrastructure.auth.tokens import generate_refresh_token, hash_refresh_token
from vault_shared import ConflictError, UnauthorizedError, get_settings
from vault_shared.db.models import Organization, RoleName, User
from vault_shared.db.repositories import (
    AuditLogRepository,
    OrganizationRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
)


@dataclass(frozen=True)
class AuthenticatedSession:
    access_token: str
    refresh_token: str
    refresh_token_id: uuid.UUID
    refresh_token_expires_at: datetime
    user: User


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "organization"


class AuthService:
    """Orchestrates the Google sign-in / session lifecycle (Handbook
    §6.1.2's application layer — no framework/HTTP concerns here, that's the
    presentation router's job)."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._users = UserRepository(db)
        self._organizations = OrganizationRepository(db)
        self._roles = RoleRepository(db)
        self._refresh_tokens = RefreshTokenRepository(db)
        self._audit_logs = AuditLogRepository(db)

    def complete_google_login(
        self, *, google_user: GoogleUserInfo, ip_address: str | None
    ) -> AuthenticatedSession:
        if not google_user.email_verified:
            # The verifier already refuses these; checked again here so no
            # other caller can create or enter an account on an unproven email.
            raise UnauthorizedError("Google sign-in failed — the account's email isn't verified.")
        user = self._users.get_by_google_sub(google_user.sub)

        if user is None:
            # Google accounts are keyed by `sub`, not email — an existing
            # account under this email but no matching `sub` is unusual
            # enough to reject rather than silently merge.
            if self._users.get_by_email(google_user.email) is not None:
                self._audit_logs.record(
                    event_type="login_failed_email_conflict",
                    metadata={"email": google_user.email},
                    ip_address=ip_address,
                )
                self._db.commit()
                raise ConflictError(
                    "An account with this email already exists under a different sign-in method."
                )
            user = self._provision_new_user(google_user, ip_address=ip_address)

        self._users.mark_login(user)
        self._audit_logs.record(
            event_type="login_success",
            organization_id=user.organization_id,
            user_id=user.id,
            ip_address=ip_address,
        )
        session = self._issue_session(user)
        self._db.commit()
        return session

    def refresh_session(
        self, *, raw_refresh_token: str, ip_address: str | None
    ) -> AuthenticatedSession:
        token = self._refresh_tokens.get_by_hash(hash_refresh_token(raw_refresh_token))

        if token is None:
            raise UnauthorizedError("Invalid session — please sign in again.")

        if token.revoked_at is not None:
            # Reuse of an already-rotated-out token is a strong signal the
            # refresh token was stolen — revoke the whole family, not just
            # reject this one request.
            self._refresh_tokens.revoke_all_for_user(token.user_id)
            self._audit_logs.record(
                event_type="refresh_token_reuse_detected",
                user_id=token.user_id,
                ip_address=ip_address,
            )
            self._db.commit()
            raise UnauthorizedError("Invalid session — please sign in again.")

        if token.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Session expired — please sign in again.")

        user = self._users.get_by_id(token.user_id)
        if user is None:
            raise UnauthorizedError("Invalid session — please sign in again.")

        session = self._issue_session(user)
        self._refresh_tokens.revoke(token, replaced_by_id=session.refresh_token_id)
        self._audit_logs.record(
            event_type="token_refreshed",
            organization_id=user.organization_id,
            user_id=user.id,
            ip_address=ip_address,
        )
        self._db.commit()
        return session

    def logout(self, *, raw_refresh_token: str, ip_address: str | None) -> None:
        token = self._refresh_tokens.get_by_hash(hash_refresh_token(raw_refresh_token))
        if token is not None and token.revoked_at is None:
            self._refresh_tokens.revoke(token)
            self._audit_logs.record(
                event_type="logout", user_id=token.user_id, ip_address=ip_address
            )
            self._db.commit()

    def _provision_new_user(self, google_user: GoogleUserInfo, *, ip_address: str | None) -> User:
        role = self._roles.get_by_name(RoleName.OWNER)
        organization = self._create_organization_for(google_user)
        user = self._users.create(
            organization_id=organization.id,
            role_id=role.id,
            google_sub=google_user.sub,
            email=google_user.email,
            name=google_user.name,
            avatar_url=google_user.picture,
        )
        self._audit_logs.record(
            event_type="organization_provisioned",
            organization_id=organization.id,
            user_id=user.id,
            metadata={"name": organization.name},
            ip_address=ip_address,
        )
        self._audit_logs.record(
            event_type="user_provisioned",
            organization_id=organization.id,
            user_id=user.id,
            metadata={"email": user.email},
            ip_address=ip_address,
        )
        return user

    def _create_organization_for(self, google_user: GoogleUserInfo) -> Organization:
        base_slug = _slugify(google_user.name or google_user.email.split("@")[0])
        slug = base_slug
        suffix = 1
        while self._organizations.get_by_slug(slug) is not None:
            suffix += 1
            slug = f"{base_slug}-{suffix}"
        name = f"{google_user.name}’s Organization" if google_user.name else "New Organization"
        return self._organizations.create(name=name, slug=slug)

    def _issue_session(self, user: User) -> AuthenticatedSession:
        settings = get_settings()
        access_token = create_access_token(
            AccessTokenClaims(
                user_id=user.id, organization_id=user.organization_id, role=user.role.name
            )
        )
        raw_refresh_token = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
        token_row = self._refresh_tokens.create(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh_token),
            expires_at=expires_at,
        )
        return AuthenticatedSession(
            access_token=access_token,
            refresh_token=raw_refresh_token,
            refresh_token_id=token_row.id,
            refresh_token_expires_at=expires_at,
            user=user,
        )
