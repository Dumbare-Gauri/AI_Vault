"""The storage workspace a request works in.

Google Drive and local storage are separate workspaces: every search,
statistic, recommendation and Ask Vault message works on exactly one
connected storage, named by `connector_id`. It is resolved here, on the
server, against the signed-in user's organization — never taken from a model,
and never "all storage".
"""

import uuid
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.presentation.dependencies.auth import get_current_user
from vault_shared import NotFoundError
from vault_shared.db.models import StorageConnector, User
from vault_shared.db.repositories import StorageConnectorRepository
from vault_shared.db.session import get_db


def resolve_workspace(
    db: Session, *, connector_id: uuid.UUID, organization_id: uuid.UUID
) -> StorageConnector:
    """The connector, if this organization owns it. Another organization's
    connector and a missing one read the same, so nothing leaks."""
    connector = StorageConnectorRepository(db).get_by_id(connector_id)
    if connector is None or connector.organization_id != organization_id:
        raise NotFoundError("That storage isn't connected to this workspace.")
    return connector


def get_workspace(
    connector_id: Annotated[
        uuid.UUID, Query(description="The connected storage this request works in.")
    ],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StorageConnector:
    return resolve_workspace(db, connector_id=connector_id, organization_id=user.organization_id)
