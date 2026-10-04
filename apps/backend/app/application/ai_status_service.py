import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider

_STUB_PROVIDER_NAME = "extractive_fallback"


@dataclass(frozen=True)
class AIStatus:
    mode: str
    source: str
    provider: str
    model: str | None


def get_ai_status(db: Session, organization_id: uuid.UUID, gateway: AIGateway) -> AIStatus:
    """What reasoning service this organization's AI features will use *right
    now*, and whether they are running in degraded (no-AI) mode. Read-only
    and offline — it reports configuration, never calls the provider, so it
    is safe to poll and reveals no key material. Live provider failures are
    surfaced by the AI features themselves, which degrade per request."""
    org_provider = resolve_org_completion_provider(db, organization_id)
    if org_provider is not None:
        return AIStatus(
            mode="ai", source="organization", provider="openrouter", model=org_provider.model_name
        )
    if gateway.completion_provider_name != _STUB_PROVIDER_NAME:
        return AIStatus(
            mode="ai",
            source="instance",
            provider="openrouter",
            model=gateway.completion_model_name,
        )
    return AIStatus(mode="degraded", source="none", provider="none", model=None)
