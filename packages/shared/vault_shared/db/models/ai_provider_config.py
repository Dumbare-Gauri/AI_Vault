import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vault_shared.db.session import Base

if TYPE_CHECKING:
    from vault_shared.db.models.organization import Organization


class AIProviderConfig(Base):
    """One organization's own AI completion provider configuration — one-
    to-one, mirrors `ConnectorCredentials`'s shape exactly (own `id` PK,
    not the FK as PK). Only ever holds an *encrypted* API key
    (`vault_shared.security.encryption`), same as OAuth tokens — never
    serialized in an API response (see `AIProviderConfigResponse`, which
    exposes only `configured`/`model_name`, never the key). Deliberately
    OpenRouter-only in this phase — no `base_url` column; the resolver
    (`ai_gateway/org_completion_provider.py`) hardcodes OpenRouter's
    endpoint, since that's the only provider choice this phase exposes in
    the UI."""

    __tablename__ = "ai_provider_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # One of `vault_shared.ai_gateway.providers_catalog.AI_PROVIDERS` — each
    # maps to a fixed, allow-listed endpoint; an organization never supplies
    # a URL of its own.
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="openrouter")
    api_key_encrypted: Mapped[str] = mapped_column(String(2048), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped["Organization"] = relationship(back_populates="ai_provider_config")
