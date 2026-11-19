import uuid

from sqlalchemy.orm import Session

from vault_shared import ValidationError
from vault_shared.ai_gateway.openrouter import check_connection
from vault_shared.ai_gateway.provider_catalog import AI_PROVIDERS
from vault_shared.db.models import AIProviderConfig
from vault_shared.db.repositories import AIProviderConfigRepository
from vault_shared.security.encryption import decrypt_token, encrypt_token


class AIProviderConfigService:
    """Per-organization AI completion provider configuration (ADR-024's
    per-org follow-up) — owner/admin-only; the provider is chosen from a
    fixed catalog (`AI_PROVIDERS`), never a free-form URL.
    Mirrors `ConnectorCredentials`' "only ever handle encrypted values at
    the model/repo layer, encrypt/decrypt at the service layer" split."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._configs = AIProviderConfigRepository(db)

    def get_status(self, organization_id: uuid.UUID) -> AIProviderConfig | None:
        return self._configs.get_by_organization_id(organization_id)

    def set(
        self,
        organization_id: uuid.UUID,
        *,
        api_key: str | None,
        model_name: str,
        provider: str,
    ) -> AIProviderConfig:
        """A blank/omitted `api_key` keeps the existing stored key,
        updating only `model_name` — lets an admin change models without
        re-pasting the key every time. Raises if there's no key to fall
        back on at all (nothing stored yet, and none given)."""
        _require_known(provider)
        existing = self._configs.get_by_organization_id(organization_id)
        if api_key:
            api_key_encrypted = encrypt_token(api_key)
        elif existing is not None and existing.provider == provider:
            api_key_encrypted = existing.api_key_encrypted
        else:
            raise ValidationError("An API key is required for this provider.")

        config = self._configs.upsert(
            organization_id=organization_id,
            api_key_encrypted=api_key_encrypted,
            model_name=model_name,
            provider=provider,
        )
        self._db.commit()
        return config

    def clear(self, organization_id: uuid.UUID) -> None:
        existing = self._configs.get_by_organization_id(organization_id)
        if existing is not None:
            self._configs.delete(existing)
            self._db.commit()

    def test(
        self,
        organization_id: uuid.UUID,
        *,
        api_key: str | None,
        model_name: str,
        provider: str,
    ) -> tuple[bool, str | None]:
        """Tests the *candidate* key/model directly — never the resolver
        (which reads only already-saved config) — so a not-yet-saved key
        can be validated before committing to it. A blank `api_key` tests
        against whatever key is already stored, matching `set()`'s "blank
        means keep/use the existing key" rule. Never persists anything,
        win or lose."""
        _require_known(provider)
        if api_key:
            resolved_key = api_key
        else:
            existing = self._configs.get_by_organization_id(organization_id)
            if existing is None or existing.provider != provider:
                return False, "No API key is saved yet — enter one to test."
            resolved_key = decrypt_token(existing.api_key_encrypted)

        return check_connection(api_key=resolved_key, model_name=model_name, provider=provider)


def _require_known(provider: str) -> None:
    if provider not in AI_PROVIDERS:
        raise ValidationError("Unknown AI provider.")
