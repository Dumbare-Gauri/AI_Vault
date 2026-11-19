import uuid

from sqlalchemy.orm import Session

from app.infrastructure.queue.scan_producer import enqueue_scan_job
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import (
    ConnectorStatus,
    ScanJob,
    ScanProgress,
    ScanStatus,
    ScanType,
    StorageConnector,
)
from vault_shared.db.repositories import (
    AuditLogRepository,
    ScanJobRepository,
    ScanProgressRepository,
    StorageConnectorRepository,
)


class ScanService:
    """Orchestrates the scan-job lifecycle from the API side: starting a
    scan enqueues work onto the worker's queue (Handbook §8.15) rather than
    running it inline — this class never touches Google Drive or the
    Folder/File tables itself, only `ScanJob`/`ScanProgress` bookkeeping and
    the connector it belongs to."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._connectors = StorageConnectorRepository(db)
        self._jobs = ScanJobRepository(db)
        self._progress = ScanProgressRepository(db)
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
    ) -> list[ScanJob]:
        self._get_owned_connector(connector_id, organization_id=organization_id)
        return self._jobs.list_for_connector(connector_id)

    def get_owned(self, scan_job_id: uuid.UUID, *, organization_id: uuid.UUID) -> ScanJob:
        job = self._jobs.get_by_id(scan_job_id)
        if job is None:
            raise NotFoundError("Scan job not found.")
        try:
            self._get_owned_connector(job.connector_id, organization_id=organization_id)
        except NotFoundError:
            # Same answer as a missing job, so another organization's job id
            # is indistinguishable from one that does not exist.
            raise NotFoundError("Scan job not found.") from None
        return job

    def get_progress(self, scan_job_id: uuid.UUID) -> ScanProgress | None:
        return self._progress.get_for_job(scan_job_id)

    def start_scan(
        self,
        connector_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        scan_type: ScanType = ScanType.FULL,
    ) -> ScanJob:
        connector = self._get_owned_connector(connector_id, organization_id=organization_id)
        if connector.status != ConnectorStatus.CONNECTED:
            raise ConflictError("Connector is not connected.")
        if self._jobs.has_active_job(connector_id):
            raise ConflictError("A scan is already pending or running for this connector.")

        job = self._jobs.create(
            connector_id=connector_id, scan_type=scan_type, triggered_by_user_id=user_id
        )
        self._audit_logs.record(
            event_type="scan_started",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector_id), "scan_type": scan_type},
        )
        self._db.commit()

        enqueue_scan_job(job.id)
        return job

    def cancel(self, job: ScanJob, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> ScanJob:
        if job.status not in (ScanStatus.PENDING, ScanStatus.RUNNING):
            raise ConflictError("Scan job is not running.")
        self._jobs.request_cancel(job)
        self._audit_logs.record(
            event_type="scan_cancel_requested",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"scan_job_id": str(job.id)},
        )
        self._db.commit()
        return job
