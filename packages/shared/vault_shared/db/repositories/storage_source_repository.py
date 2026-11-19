import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import StorageSource


class StorageSourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, storage_source_id: uuid.UUID) -> StorageSource | None:
        return self._session.get(StorageSource, storage_source_id)

    def get_by_connector_and_provider_drive_id(
        self, *, connector_id: uuid.UUID, provider_drive_id: str
    ) -> StorageSource | None:
        return (
            self._session.query(StorageSource)
            .filter_by(connector_id=connector_id, provider_drive_id=provider_drive_id)
            .first()
        )

    def list_for_connector(self, connector_id: uuid.UUID) -> list[StorageSource]:
        return self._session.query(StorageSource).filter_by(connector_id=connector_id).all()

    def upsert(
        self, *, connector_id: uuid.UUID, provider_drive_id: str, name: str, drive_type: str
    ) -> StorageSource:
        source = self.get_by_connector_and_provider_drive_id(
            connector_id=connector_id, provider_drive_id=provider_drive_id
        )
        if source is None:
            source = StorageSource(
                connector_id=connector_id,
                provider_drive_id=provider_drive_id,
                drive_type=drive_type,
            )
            self._session.add(source)
        source.name = name
        source.drive_type = drive_type
        self._session.flush()
        return source

    def update_change_token(self, source: StorageSource, *, change_token: str | None) -> None:
        source.change_token = change_token
        self._session.flush()
