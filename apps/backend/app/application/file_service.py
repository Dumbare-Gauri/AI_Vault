import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy.orm import Session

from vault_shared import NotFoundError
from vault_shared.db.models import (
    File,
    FileClassification,
    FileExtraction,
    FileIntelligence,
    FileMetadata,
    FileRelationship,
    KnowledgeAttribute,
)
from vault_shared.db.repositories import (
    FileClassificationRepository,
    FileExtractionRepository,
    FileIntelligenceRepository,
    FileMetadataRepository,
    FileRelationshipRepository,
    FileRepository,
    KnowledgeAttributeRepository,
    StorageConnectorRepository,
)
from vault_shared.storage import ExportPurpose, ProviderFileId, StorageAdapterProvider


@dataclass(frozen=True)
class RelatedFile:
    file: File
    relationship: FileRelationship


@dataclass(frozen=True)
class FileDetail:
    file: File
    metadata: FileMetadata | None
    classification: FileClassification | None
    extraction: FileExtraction | None
    intelligence: FileIntelligence | None
    knowledge_attributes: list[KnowledgeAttribute]
    related_files: list[RelatedFile]


class FileService:
    """Mostly read-only — the Knowledge Engine's persistence layer
    (`FileMetadata`, `FileClassification`, etc.) is written exclusively by
    `apps/worker`'s `EnrichmentService`; the backend only ever assembles and
    serves what's already there for the frontend's file detail view
    (Phase 5 spec's Frontend Deliverables). `get_download_stream` is the one
    exception that reaches all the way to the storage provider (through the
    connection's `StorageAdapter`) — still not a mutation, so
    it doesn't go through the Execution Engine (whose charter is "the only
    module permitted to *mutate* connected storage"), same reasoning as
    `ArchiveService.get_download_stream`."""

    def __init__(self, db: Session, *, storage: StorageAdapterProvider) -> None:
        self._storage = storage
        self._connectors = StorageConnectorRepository(db)
        self._files = FileRepository(db)
        self._file_metadata = FileMetadataRepository(db)
        self._classifications = FileClassificationRepository(db)
        self._extractions = FileExtractionRepository(db)
        self._intelligence = FileIntelligenceRepository(db)
        self._knowledge_attributes = KnowledgeAttributeRepository(db)
        self._relationships = FileRelationshipRepository(db)

    def list_for_connector(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        limit: int,
        offset: int,
        ownership: str | None = None,
    ) -> tuple[list[File], int]:
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connector not found.")
        files = self._files.list_for_connector(
            connector_id, limit=limit, offset=offset, ownership=ownership
        )
        total = self._files.count_for_connector(connector_id, ownership=ownership)
        return files, total

    def search_folders(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, query: str, limit: int
    ) -> list[File]:
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connector not found.")
        return self._files.search_folders_for_connector(connector_id, query=query, limit=limit)

    def list_trashed_for_connector(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[File], int]:
        """Backs the Trash browse view — files this platform has already
        moved to Drive's Trash (ARCHIVE/REMOVE_DUPLICATE) and not yet
        permanently deleted. Google keeps counting these against the
        account's real storage quota until they're emptied from Trash, so
        this is where "Permanently delete" is offered."""
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connector not found.")
        files = self._files.list_trashed_for_connector(connector_id, limit=limit, offset=offset)
        total = self._files.count_trashed_for_connector(connector_id)
        return files, total

    def get_detail(self, file_id: uuid.UUID, *, organization_id: uuid.UUID) -> FileDetail:
        file = self._files.get_owned_by_organization(file_id, organization_id=organization_id)
        if file is None:
            raise NotFoundError("File not found.")

        relationship_rows = self._relationships.list_for_file(file.id)

        def other_side(row: FileRelationship) -> uuid.UUID:
            return row.related_file_id if row.file_id == file.id else row.file_id

        related_ids = {other_side(row) for row in relationship_rows}
        related_files_by_id = {f.id: f for f in self._files.list_by_ids(list(related_ids))}

        related_files = [
            RelatedFile(file=related_files_by_id[other_side(row)], relationship=row)
            for row in relationship_rows
            if other_side(row) in related_files_by_id
        ]

        return FileDetail(
            file=file,
            metadata=self._file_metadata.get_by_file_id(file.id),
            classification=self._classifications.get_by_file_id(file.id),
            extraction=self._extractions.get_by_file_id(file.id),
            intelligence=self._intelligence.get_by_file_id(file.id),
            knowledge_attributes=self._knowledge_attributes.list_for_file(file.id),
            related_files=related_files,
        )

    def get_download_stream(
        self, file_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> tuple[File, Iterator[bytes], str, str]:
        """Returns `(file, byte_stream, content_type, filename)`. A
        provider-native document (a Google Doc/Sheet/Slide) has no binary
        "current version" to stream — the adapter exports it to a real,
        openable format instead, and `filename` reflects that (e.g. `.pdf`
        for a Doc), never the original native name unchanged.

        The stream is the provider's own response, read in bounded chunks as
        the client consumes it — never buffered whole. The request is made
        here, so a provider refusal still surfaces as a normal error before
        any response bytes are sent."""
        row = self._files.get_owned_with_connector(file_id, organization_id=organization_id)
        if row is None:
            raise NotFoundError("File not found.")
        file, connector = row
        adapter = self._storage.adapter_for(connector)

        mime = file.mime_type or ""
        provider_file_id = ProviderFileId(file.provider_file_id)
        export_format = adapter.export_format_for(mime, ExportPurpose.DOWNLOAD)
        if export_format is not None:
            content = adapter.export(provider_file_id, export_format)
            filename = f"{file.name}.{export_format.extension}"
            content_type = export_format.mime_type
        elif adapter.is_native_document(mime):
            raise NotFoundError("This file type can't be downloaded.")
        else:
            content = adapter.open_read(provider_file_id)
            filename = file.name
            content_type = mime or "application/octet-stream"

        return file, content, content_type, filename
