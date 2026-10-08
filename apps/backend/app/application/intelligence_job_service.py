import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.intelligence_producer import enqueue_intelligence_job
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import (
    ConnectorStatus,
    IntelligenceJob,
    IntelligenceJobStatus,
    IntelligenceProgress,
    IntelligenceTrigger,
    StorageConnector,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    IntelligenceJobRepository,
    IntelligenceProgressRepository,
    StorageConnectorRepository,
)


class IntelligenceJobService:
    """Orchestrates the intelligence-job lifecycle from the API side —
    literal mirror of `EnrichmentJobService`. Manual re-analysis (this
    class's `start`) is the same kind of job the enrichment-completion
    chain (`worker.tasks.enrichment._enqueue_intelligence_if_enrichment_completed`)
    creates automatically; this is the user-triggered path (e.g. after
    configuring a completion provider for the first time, to analyze
    already-enriched files without waiting for the next scan)."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connectors = StorageConnectorRepository(db)
        self._jobs = IntelligenceJobRepository(db)
        self._progress = IntelligenceProgressRepository(db)
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
    ) -> list[IntelligenceJob]:
        self._get_owned_connector(connector_id, organization_id=organization_id)
        return self._jobs.list_for_connector(connector_id)

    def get_owned(
        self, intelligence_job_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> IntelligenceJob:
        job = self._jobs.get_by_id(intelligence_job_id)
        if job is None:
            raise NotFoundError("Intelligence job not found.")
        try:
            self._get_owned_connector(job.connector_id, organization_id=organization_id)
        except NotFoundError:
            raise NotFoundError("Intelligence job not found.") from None
        return job

    def get_progress(self, intelligence_job_id: uuid.UUID) -> IntelligenceProgress | None:
        return self._progress.get_for_job(intelligence_job_id)

    def start(
        self, connector_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> IntelligenceJob:
        connector = self._get_owned_connector(connector_id, organization_id=organization_id)
        if connector.status != ConnectorStatus.CONNECTED:
            raise ConflictError("Connector is not connected.")
        if self._jobs.has_active_job(connector_id):
            raise ConflictError(
                "An intelligence job is already pending or running for this connector."
            )

        job = self._jobs.create(
            connector_id=connector_id,
            triggered_by=IntelligenceTrigger.MANUAL,
            triggered_by_user_id=user_id,
        )
        self._audit_logs.record(
            event_type="intelligence_started",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector_id)},
        )
        self._db.commit()

        enqueue_intelligence_job(job.id)
        return job

    def cancel(
        self, job: IntelligenceJob, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> IntelligenceJob:
        if job.status not in (IntelligenceJobStatus.PENDING, IntelligenceJobStatus.RUNNING):
            raise ConflictError("Intelligence job is not running.")
        self._jobs.request_cancel(job)
        self._audit_logs.record(
            event_type="intelligence_cancel_requested",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"intelligence_job_id": str(job.id)},
        )
        self._db.commit()
        return job
