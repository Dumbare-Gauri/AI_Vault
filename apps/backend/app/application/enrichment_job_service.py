import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.enrichment_producer import enqueue_enrichment_job
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import (
    ConnectorStatus,
    EnrichmentJob,
    EnrichmentJobStatus,
    EnrichmentProgress,
    EnrichmentTrigger,
    StorageConnector,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    EnrichmentJobRepository,
    EnrichmentProgressRepository,
    StorageConnectorRepository,
)


class EnrichmentJobService:
    """Orchestrates the enrichment-job lifecycle from the API side — mirrors
    `ScanService` exactly. Manual re-enrichment (this class's `start`) is
    the same kind of job the scan-completion chain
    (`worker.tasks.scan._enqueue_enrichment_if_scan_completed`) creates
    automatically; this is just the user-triggered path the phase spec's
    "reprocessing updated files" manual QA step needs."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connectors = StorageConnectorRepository(db)
        self._jobs = EnrichmentJobRepository(db)
        self._progress = EnrichmentProgressRepository(db)
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
    ) -> list[EnrichmentJob]:
        self._get_owned_connector(connector_id, organization_id=organization_id)
        return self._jobs.list_for_connector(connector_id)

    def get_owned(
        self, enrichment_job_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> EnrichmentJob:
        job = self._jobs.get_by_id(enrichment_job_id)
        if job is None:
            raise NotFoundError("Enrichment job not found.")
        try:
            self._get_owned_connector(job.connector_id, organization_id=organization_id)
        except NotFoundError:
            raise NotFoundError("Enrichment job not found.") from None
        return job

    def get_progress(self, enrichment_job_id: uuid.UUID) -> EnrichmentProgress | None:
        return self._progress.get_for_job(enrichment_job_id)

    def start(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> EnrichmentJob:
        connector = self._get_owned_connector(connector_id, organization_id=organization_id)
        if connector.status != ConnectorStatus.CONNECTED:
            raise ConflictError("Connector is not connected.")
        if self._jobs.has_active_job(connector_id):
            raise ConflictError(
                "An enrichment job is already pending or running for this connector."
            )

        job = self._jobs.create(
            connector_id=connector_id,
            triggered_by=EnrichmentTrigger.MANUAL,
            triggered_by_user_id=user_id,
        )
        self._audit_logs.record(
            event_type="enrichment_started",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector_id)},
        )
        self._db.commit()

        enqueue_enrichment_job(job.id)
        return job

    def cancel(
        self, job: EnrichmentJob, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> EnrichmentJob:
        if job.status not in (EnrichmentJobStatus.PENDING, EnrichmentJobStatus.RUNNING):
            raise ConflictError("Enrichment job is not running.")
        self._jobs.request_cancel(job)
        self._audit_logs.record(
            event_type="enrichment_cancel_requested",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"enrichment_job_id": str(job.id)},
        )
        self._db.commit()
        return job
