import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import AIProviderConfig


class AIProviderConfigRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_organization_id(self, organization_id: uuid.UUID) -> AIProviderConfig | None:
        return (
            self._session.query(AIProviderConfig).filter_by(organization_id=organization_id).first()
        )

    def upsert(
        self,
        *,
        organization_id: uuid.UUID,
        api_key_encrypted: str,
        model_name: str,
        provider: str,
    ) -> AIProviderConfig:
        config = self.get_by_organization_id(organization_id)
        if config is None:
            config = AIProviderConfig(organization_id=organization_id)
            self._session.add(config)

        config.api_key_encrypted = api_key_encrypted
        config.model_name = model_name
        config.provider = provider
        self._session.flush()
        return config

    def delete(self, config: AIProviderConfig) -> None:
        self._session.delete(config)
        self._session.flush()
