"""The composition root for storage: the one place that knows which providers
exist and how to build their adapters.

Only backend dependency wiring and worker task entry points import this.
Business services take a `StorageAdapterProvider` and never construct a
provider client themselves.
"""

from sqlalchemy.orm import Session

from vault_shared.connectors.google_drive import GoogleDriveClient
from vault_shared.connectors.google_workspace import GoogleWorkspaceOAuthClient
from vault_shared.db.models import ConnectorProvider, StorageConnector
from vault_shared.db.session import get_session_factory
from vault_shared.storage.adapter import StorageAdapter
from vault_shared.storage.adapters.google_drive import GoogleDriveAdapter
from vault_shared.storage.adapters.local_agent import LocalAgentAdapter
from vault_shared.storage.connector_credentials import ConnectorCredentialSource
from vault_shared.storage.registry import StorageAdapterRegistry


def build_storage_registry(
    db: Session,
    *,
    oauth_client: GoogleWorkspaceOAuthClient,
    drive_client: GoogleDriveClient | None = None,
) -> StorageAdapterRegistry:
    """A registry bound to `db` with every implemented provider registered.

    `drive_client` exists so tests can substitute a fake HTTP client while
    still running the real adapter code. Adding a provider means registering
    one more factory here.
    """
    client = drive_client if drive_client is not None else GoogleDriveClient()

    def google_drive(connector: StorageConnector) -> StorageAdapter:
        return GoogleDriveAdapter(
            client=client,
            credentials=ConnectorCredentialSource(db, connector, oauth_client=oauth_client),
            connection_id=connector.id,
        )

    def local_agent(connector: StorageConnector) -> StorageAdapter:
        return LocalAgentAdapter(connector=connector, session_factory=get_session_factory())

    registry = StorageAdapterRegistry()
    registry.register(ConnectorProvider.GOOGLE_WORKSPACE.value, google_drive)
    registry.register(ConnectorProvider.LOCAL_AGENT.value, local_agent)
    return registry
