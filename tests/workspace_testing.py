"""The storage workspace a test fixture's record belongs to.

Recommendations, duplicate groups and conversations are recorded per
connected storage. A fixture that only cares about an organization uses the
organization's existing connection, or a fresh Google Drive one if it has
none yet.
"""

import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import ConnectorProvider, StorageConnector
from vault_shared.db.repositories import StorageConnectorRepository


def workspace_of(db: Session, organization_id: uuid.UUID) -> StorageConnector:
    connectors = StorageConnectorRepository(db)
    existing = connectors.list_for_organization(organization_id)
    if existing:
        return existing[0]
    connector = connectors.upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=None,
        account_email=None,
        workspace_domain=None,
    )
    db.flush()
    return connector
