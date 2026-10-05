import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from vault_shared.db.like import escape_like
from vault_shared.db.models import Folder, StorageConnector, StorageSource


class FolderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_source_and_provider_id(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str
    ) -> Folder | None:
        return (
            self._session.query(Folder)
            .filter_by(storage_source_id=storage_source_id, provider_file_id=provider_file_id)
            .first()
        )

    def get_by_id(self, folder_id: uuid.UUID) -> Folder | None:
        return self._session.get(Folder, folder_id)

    def search_for_connector(
        self, connector_id: uuid.UUID, *, query: str, limit: int
    ) -> list[Folder]:
        """Backs the "Move to folder" picker. Folders live in this table,
        never in `files` — the scanner stores them separately."""
        pattern = f"%{escape_like(query.strip())}%"
        return (
            self._session.query(Folder)
            .join(StorageSource, Folder.storage_source_id == StorageSource.id)
            .filter(StorageSource.connector_id == connector_id)
            .filter(Folder.name.ilike(pattern, escape="\\"))
            .order_by(Folder.path)
            .limit(limit)
            .all()
        )

    def get_by_source_and_provider_parent(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str | None
    ) -> Folder | None:
        if not provider_file_id:
            return None
        return self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )

    def get_by_parent_and_name(
        self, *, storage_source_id: uuid.UUID, parent_folder_id: uuid.UUID | None, name: str
    ) -> Folder | None:
        """Used by `worker.organization.
        organization_recommendation_apply_service` to walk a suggested
        destination path component by component, creating only the
        folders that don't already exist. Matches by name, case-
        sensitively, same as Drive's own folder names."""
        return (
            self._session.query(Folder)
            .filter_by(
                storage_source_id=storage_source_id,
                parent_folder_id=parent_folder_id,
                name=name,
            )
            .first()
        )

    def list_by_ids(self, folder_ids: list[uuid.UUID]) -> list[Folder]:
        """Used by `worker.organization.organization_recommendation_generator`
        to resolve an entity's files' `parent_folder_id`s into human-readable
        paths for `current_locations` in one query rather than N."""
        if not folder_ids:
            return []
        return self._session.query(Folder).filter(Folder.id.in_(folder_ids)).all()

    def upsert(
        self,
        *,
        storage_source_id: uuid.UUID,
        provider_file_id: str,
        provider_parent_id: str | None,
        parent_folder_id: uuid.UUID | None,
        name: str,
        path: str,
        owner_email: str | None,
        is_shared: bool,
        provider_created_at: datetime | None,
        provider_modified_at: datetime | None,
        scanned_at: datetime,
    ) -> Folder:
        folder = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if folder is None:
            folder = Folder(storage_source_id=storage_source_id, provider_file_id=provider_file_id)
            self._session.add(folder)

        folder.provider_parent_id = provider_parent_id
        folder.parent_folder_id = parent_folder_id
        folder.name = name
        folder.path = path
        folder.owner_email = owner_email
        folder.is_shared = is_shared
        folder.provider_created_at = provider_created_at
        folder.provider_modified_at = provider_modified_at
        folder.scanned_at = scanned_at
        self._session.flush()
        return folder

    def count_for_source(self, storage_source_id: uuid.UUID) -> int:
        return self._session.query(Folder).filter_by(storage_source_id=storage_source_id).count()

    def count_for_organization(self, organization_id: uuid.UUID) -> int:
        return (
            self._session.query(Folder)
            .join(StorageSource, Folder.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(StorageConnector.organization_id == organization_id)
            .count()
        )

    def delete_by_source_and_provider_id(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str
    ) -> bool:
        """Used by incremental sync when Drive reports an item removed or
        trashed; the FK's ON DELETE CASCADE handles any descendant folders/
        files still pointing at this one. Returns whether a row existed."""
        folder = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if folder is None:
            return False
        self._session.delete(folder)
        self._session.flush()
        return True

    def list_for_source(self, storage_source_id: uuid.UUID) -> list[Folder]:
        """Used only by the scanner's post-ingest hierarchy-resolution pass
        (`ScannerService._resolve_hierarchy`) — folders are a small subset
        of items even at reference scale (Handbook §20), so holding all of
        one source's folders in memory for that bounded pass is acceptable;
        `File` rows are never listed this way."""
        return self._session.query(Folder).filter_by(storage_source_id=storage_source_id).all()
