import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.embedding_producer import enqueue_embedding_job
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import (
    ConnectorStatus,
    EmbeddingJob,
    EmbeddingJobStatus,
    EmbeddingProgress,
    EmbeddingTrigger,
    StorageConnector,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    EmbeddingJobRepository,
    EmbeddingProgressRepository,
    StorageConnectorRepository,
)


class EmbeddingJobService:
    """Orchestrates the embedding-job lifecycle from the API side — mirrors
    `EnrichmentJobService`/`ScanService` exactly. Manual re-embedding (this
    class's `start`) is the same kind of job the enrichment-completion chain
    (`worker.tasks.enrichment._enqueue_embedding_if_enrichment_completed`)
    creates automatically; this is just the user-triggered path (e.g. after
    changing the embedding provider) for the phase spec's "detect when
    documents require re-embedding" to be re-run on demand."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connectors = StorageConnectorRepository(db)
        self._jobs = EmbeddingJobRepository(db)
        self._progress = EmbeddingProgressRepository(db)
        self._audit_logs = AuditLogRepository(db)

    def _get_owned_connector(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> StorageConnector:
        connector = self._connectors.get_by_id(connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError("Connector not found.")
        return connector

    def list_for_connector(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> list[EmbeddingJob]:
        self._get_owned_connector(connector_id, organization_id=organization_id)
        return self._jobs.list_for_connector(connector_id)

    def get_owned(self, embedding_job_id: uuid.UUID, *, organization_id: uuid.UUID) -> EmbeddingJob:
        job = self._jobs.get_by_id(embedding_job_id)
        if job is None:
            raise NotFoundError("Embedding job not found.")
        try:
            self._get_owned_connector(job.connector_id, organization_id=organization_id)
        except NotFoundError:
            raise NotFoundError("Embedding job not found.") from None
        return job

    def get_progress(self, embedding_job_id: uuid.UUID) -> EmbeddingProgress | None:
        return self._progress.get_for_job(embedding_job_id)

    def start(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> EmbeddingJob:
        connector = self._get_owned_connector(connector_id, organization_id=organization_id)
        if connector.status != ConnectorStatus.CONNECTED:
            raise ConflictError("Connector is not connected.")
        if self._jobs.has_active_job(connector_id):
            raise ConflictError(
                "An embedding job is already pending or running for this connector."
            )

        job = self._jobs.create(
            connector_id=connector_id,
            triggered_by=EmbeddingTrigger.MANUAL,
            triggered_by_user_id=user_id,
        )
        self._audit_logs.record(
            event_type="embedding_started",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector_id)},
        )
        self._db.commit()

        enqueue_embedding_job(job.id)
        return job

    def cancel(
        self, job: EmbeddingJob, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> EmbeddingJob:
        if job.status not in (EmbeddingJobStatus.PENDING, EmbeddingJobStatus.RUNNING):
            raise ConflictError("Embedding job is not running.")
        self._jobs.request_cancel(job)
        self._audit_logs.record(
            event_type="embedding_cancel_requested",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"embedding_job_id": str(job.id)},
        )
        self._db.commit()
        return job
