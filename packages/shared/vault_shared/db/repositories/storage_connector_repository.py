import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import ConnectorStatus, StorageConnector


class StorageConnectorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, connector_id: uuid.UUID) -> StorageConnector | None:
        return self._session.get(StorageConnector, connector_id)

    def get_by_organization_and_provider(
        self, *, organization_id: uuid.UUID, provider: str
    ) -> StorageConnector | None:
        return (
            self._session.query(StorageConnector)
            .filter_by(organization_id=organization_id, provider=provider)
            .first()
        )

    def list_for_organization(self, organization_id: uuid.UUID) -> list[StorageConnector]:
        return (
            self._session.query(StorageConnector)
            .filter_by(organization_id=organization_id)
            .order_by(StorageConnector.created_at)
            .all()
        )

    def upsert_connected(
        self,
        *,
        organization_id: uuid.UUID,
        provider: str,
        connected_by_user_id: uuid.UUID,
        account_email: str | None,
        workspace_domain: str | None,
    ) -> StorageConnector:
        """Creates the connector row on first connect, or reuses the same row
        on reconnect — one row per (organization, provider), never a growing
        history of duplicate attempts."""
        connector = self.get_by_organization_and_provider(
            organization_id=organization_id, provider=provider
        )
        if connector is None:
            connector = StorageConnector(organization_id=organization_id, provider=provider)
            self._session.add(connector)

        connector.status = ConnectorStatus.CONNECTED
        connector.connected_by_user_id = connected_by_user_id
        connector.account_email = account_email
        connector.workspace_domain = workspace_domain
        connector.last_verified_at = datetime.now(UTC)
        connector.last_failed_at = None
        connector.last_error = None
        self._session.flush()
        return connector

    def mark_verified(self, connector: StorageConnector) -> None:
        connector.status = ConnectorStatus.CONNECTED
        connector.last_verified_at = datetime.now(UTC)
        connector.last_error = None
        self._session.flush()

    def mark_error(self, connector: StorageConnector, *, error: str) -> None:
        connector.status = ConnectorStatus.ERROR
        connector.last_failed_at = datetime.now(UTC)
        connector.last_error = error[:1024]
        self._session.flush()

    def mark_reauth_required(self, connector: StorageConnector, *, error: str) -> None:
        connector.status = ConnectorStatus.REAUTH_REQUIRED
        connector.last_failed_at = datetime.now(UTC)
        connector.last_error = error[:1024]
        self._session.flush()

    def mark_disconnected(self, connector: StorageConnector) -> None:
        connector.status = ConnectorStatus.DISCONNECTED
        self._session.flush()
