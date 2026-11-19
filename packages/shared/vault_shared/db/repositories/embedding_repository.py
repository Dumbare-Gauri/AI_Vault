import uuid
from datetime import datetime

from sqlalchemy.orm import Query, Session

from vault_shared.db.models import Embedding, File, StorageConnector, StorageSource


class EmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_file_id(self, file_id: uuid.UUID) -> Embedding | None:
        return self._session.get(Embedding, file_id)

    def upsert(
        self,
        *,
        file_id: uuid.UUID,
        model_name: str,
        model_version: str,
        dimensions: int,
        vector: list[float],
        content_hash: str,
        embedded_at: datetime,
    ) -> Embedding:
        embedding = self.get_by_file_id(file_id)
        if embedding is None:
            embedding = Embedding(file_id=file_id)
            self._session.add(embedding)

        embedding.model_name = model_name
        embedding.model_version = model_version
        embedding.dimensions = dimensions
        embedding.vector = vector
        embedding.content_hash = content_hash
        embedding.embedded_at = embedded_at
        self._session.flush()
        return embedding

    def list_for_connector(
        self, connector_id: uuid.UUID, *, model: tuple[str, str] | None = None
    ) -> list[tuple[File, Embedding]]:
        """Connector-scoped counterpart to `list_for_organization` — used
        by `worker.relationships.near_duplicate_service`, which runs per
        `EmbeddingJob` (connector-scoped) rather than per organization."""
        query = (
            self._session.query(File, Embedding)
            .join(Embedding, Embedding.file_id == File.id)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .filter(StorageSource.connector_id == connector_id)
        )
        return [(file, embedding) for file, embedding in _of_model(query, model).all()]

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        model: tuple[str, str] | None = None,
        connector_id: uuid.UUID | None = None,
    ) -> list[tuple[File, Embedding]]:
        """Returns every (File, Embedding) pair in the organization — the
        candidate set `SearchService` ranks by cosine similarity against a
        query vector. Brute-force by design (Handbook §9/§22 — a dedicated
        vector store is a future-scale concern, not solved here); fine at
        reference scale, and never leaks another organization's files since
        the join goes all the way to `storage_connectors.organization_id`.

        `model` — `(model_name, model_version)` — keeps only vectors from
        that embedding space. Vectors from two models are not comparable, and
        while a re-embed after a model change is still running both kinds
        sit side by side."""
        query = (
            self._session.query(File, Embedding)
            .join(Embedding, Embedding.file_id == File.id)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(StorageConnector.organization_id == organization_id)
        )
        if connector_id is not None:
            query = query.filter(StorageConnector.id == connector_id)
        return [(file, embedding) for file, embedding in _of_model(query, model).all()]


def _of_model(query: Query, model: tuple[str, str] | None) -> Query:
    if model is None:
        return query
    name, version = model
    return query.filter(Embedding.model_name == name, Embedding.model_version == version)
