import uuid

from sqlalchemy.orm import Session

from vault_shared import get_logger
from vault_shared.ai_gateway.interfaces import CompletionProvider
from vault_shared.ai_gateway.openrouter import build_catalog_provider
from vault_shared.db.repositories import AIProviderConfigRepository
from vault_shared.security.encryption import TokenEncryptionError, decrypt_token

logger = get_logger("vault_shared.ai_gateway.org_completion_provider")


def resolve_org_completion_provider(
    db: Session, organization_id: uuid.UUID
) -> CompletionProvider | None:
    """`None` when the org has no configured provider, or when its stored
    key can't be decrypted (e.g. `CONNECTOR_ENCRYPTION_KEY` rotated) —
    never raises. Callers bind the result via `AIGateway.
    with_completion_provider()` when non-`None`, or simply keep using the
    instance-wide default gateway when `None` — this function never
    fabricates a fallback provider itself, so the caller's own default
    stays the single source of truth for "what happens with no org
    config." Resolved fresh from the DB on every call (constructing the
    provider is cheap — no model loading), so an updated key takes effect
    on the very next request with no cache to invalidate. The base URL is
    always the chosen catalog provider's fixed endpoint, never
    organization-controlled."""
    config = AIProviderConfigRepository(db).get_by_organization_id(organization_id)
    if config is None:
        return None

    try:
        api_key = decrypt_token(config.api_key_encrypted)
    except TokenEncryptionError:
        logger.warning(
            "org_ai_provider_key_decrypt_failed", extra={"organization_id": str(organization_id)}
        )
        return None

    return build_catalog_provider(
        provider=config.provider, api_key=api_key, model_name=config.model_name
    )
