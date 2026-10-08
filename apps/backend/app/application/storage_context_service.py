"""What AI Vault knows about each connected storage, as plain facts.

Ask Vault reasons over these — which storages exist, how much of each is
indexed, what the provider itself reports, how fresh the index is and which
operations the storage really supports — without ever touching an adapter
or a credential itself.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared.db.models import (
    ConnectorProvider,
    ConnectorStatus,
    StorageConnector,
    provider_display_name,
)
from vault_shared.db.repositories import (
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.storage import Capability
from vault_shared.storage.registry import StorageAdapterProvider

STALE_AFTER = timedelta(hours=12)
_OPERATION_NAMES = {
    Capability.CREATE_FOLDER: "create folders",
    Capability.COPY: "copy",
    Capability.TRASH: "move to Trash",
    Capability.RESTORE: "restore from Trash",
    Capability.PERMANENT_DELETE: "delete permanently",
    Capability.DOWNLOAD: "open and read files",
}


@dataclass(frozen=True)
class StorageFacts:
    connector_id: uuid.UUID
    label: str
    is_local: bool
    status: str
    indexed_files: int | None
    indexed_bytes: int | None
    potential_savings_bytes: int | None
    duplicate_recoverable_bytes: int | None
    duplicate_group_count: int | None
    breakdown_by_type_bytes: dict[str, int]
    provider_used_bytes: int | None
    provider_total_bytes: int | None
    provider_trash_bytes: int | None
    last_synced_at: datetime | None
    operations: tuple[str, ...]
    can_archive: bool

    @property
    def stale(self) -> bool:
        return self.last_synced_at is None or datetime.now(UTC) - self.last_synced_at > STALE_AFTER


def storage_label(connector: StorageConnector) -> str:
    if connector.provider == ConnectorProvider.LOCAL_AGENT:
        return connector.display_name or "My computer"
    return provider_display_name(connector.provider)


class StorageContextService:
    def __init__(self, db: Session, *, registry: StorageAdapterProvider | None = None) -> None:
        self._db = db
        self._registry = registry
        self._connectors = StorageConnectorRepository(db)
        self._sources = StorageSourceRepository(db)
        self._snapshots = StorageAnalysisSnapshotRepository(db)

    def connected(self, organization_id: uuid.UUID) -> list[StorageConnector]:
        return [
            connector
            for connector in self._connectors.list_for_organization(organization_id)
            if connector.status == ConnectorStatus.CONNECTED
        ]

    def source_owners(self, organization_id: uuid.UUID) -> dict[uuid.UUID, StorageConnector]:
        """storage_source_id → the connected storage it belongs to."""
        return {
            source.id: connector
            for connector in self._connectors.list_for_organization(organization_id)
            for source in self._sources.list_for_connector(connector.id)
        }

    def facts(self, organization_id: uuid.UUID) -> list[StorageFacts]:
        connected = self.connected(organization_id)
        return [
            self._facts_for(organization_id, c, only=len(connected) == 1) for c in connected
        ]

    def _facts_for(
        self, organization_id: uuid.UUID, connector: StorageConnector, *, only: bool
    ) -> StorageFacts:
        snapshot = self._snapshots.get_latest_for_organization(
            organization_id, connector_id=connector.id
        )
        if snapshot is None and only:
            # Analyzed before per-storage snapshots existed: with a single
            # storage, the organization's figures are that storage's.
            snapshot = self._snapshots.get_latest_for_organization(organization_id)
        is_local = connector.provider == ConnectorProvider.LOCAL_AGENT
        return StorageFacts(
            connector_id=connector.id,
            label=storage_label(connector),
            is_local=is_local,
            status=connector.status,
            indexed_files=snapshot.total_files if snapshot else None,
            indexed_bytes=snapshot.total_size_bytes if snapshot else None,
            potential_savings_bytes=snapshot.total_potential_savings_bytes if snapshot else None,
            duplicate_recoverable_bytes=snapshot.duplicate_recoverable_bytes if snapshot else None,
            duplicate_group_count=snapshot.duplicate_group_count if snapshot else None,
            breakdown_by_type_bytes=dict(snapshot.breakdown_by_type_bytes) if snapshot else {},
            provider_used_bytes=connector.storage_used_bytes,
            provider_total_bytes=connector.storage_total_bytes,
            provider_trash_bytes=connector.storage_trash_bytes,
            last_synced_at=connector.last_synced_at,
            operations=self._operations(connector),
            # Zipping needs a binary upload, which the computer agent doesn't do yet.
            can_archive=not is_local,
        )

    def _operations(self, connector: StorageConnector) -> tuple[str, ...]:
        names = ["search", "rename", "move"]
        if self._registry is None:
            return tuple(names)
        capabilities = self._registry.adapter_for(connector).capabilities()
        names += [name for cap, name in _OPERATION_NAMES.items() if capabilities.supports(cap)]
        return tuple(names)
