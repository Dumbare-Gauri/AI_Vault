import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from vault_shared.db.models import (
    File,
    FileIntelligence,
    IntelligenceStatus,
    StorageConnector,
    StorageSource,
)


def _organization_files(organization_id: uuid.UUID) -> Select[uuid.UUID]:
    return (
        select(File.id)
        .join(StorageSource, StorageSource.id == File.storage_source_id)
        .join(StorageConnector, StorageConnector.id == StorageSource.connector_id)
        .where(
            StorageConnector.organization_id == organization_id,
            File.trashed.is_(False),
            File.permanently_deleted_at.is_(None),
        )
    )


class FileIntelligenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_file_id(self, file_id: uuid.UUID) -> FileIntelligence | None:
        return self._session.get(FileIntelligence, file_id)

    def upsert(
        self,
        *,
        file_id: uuid.UUID,
        status: str,
        document_type: str | None,
        summary: str | None,
        entities: list,
        structured_metadata: dict,
        topics: list,
        confidence: float | None,
        provider: str,
        model_name: str,
        error: str | None,
        processed_at: datetime,
    ) -> FileIntelligence:
        intelligence = self.get_by_file_id(file_id)
        if intelligence is None:
            intelligence = FileIntelligence(file_id=file_id)
            self._session.add(intelligence)

        intelligence.status = status
        intelligence.document_type = document_type
        intelligence.summary = summary
        intelligence.entities = entities
        intelligence.structured_metadata = structured_metadata
        intelligence.topics = topics
        intelligence.confidence = confidence
        intelligence.provider = provider
        intelligence.model_name = model_name
        intelligence.error = error
        intelligence.processed_at = processed_at
        self._session.flush()
        return intelligence

    def list_successful_for_organization(
        self, organization_id: uuid.UUID
    ) -> list[tuple[File, FileIntelligence]]:
        rows = self._session.execute(
            select(File, FileIntelligence)
            .join(FileIntelligence, FileIntelligence.file_id == File.id)
            .where(
                File.id.in_(_organization_files(organization_id)),
                FileIntelligence.status == IntelligenceStatus.SUCCESS,
            )
        ).all()
        return [(file, intelligence) for file, intelligence in rows]

    def count_analyzed_for_organization(self, organization_id: uuid.UUID) -> int:
        return self._session.execute(
            select(func.count())
            .select_from(FileIntelligence)
            .where(
                FileIntelligence.file_id.in_(_organization_files(organization_id)),
                FileIntelligence.status == IntelligenceStatus.SUCCESS,
            )
        ).scalar_one()
