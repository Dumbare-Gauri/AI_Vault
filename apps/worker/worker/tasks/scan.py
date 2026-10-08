import uuid

from celery import Task
from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, get_logger
from vault_shared.connectors.google_workspace import get_google_workspace_oauth_client
from vault_shared.db.models import (
    EnrichmentTrigger,
    ScanStatus,
    StorageAnalysisTrigger,
    WorkflowEventType,
)
from vault_shared.db.repositories import (
    EnrichmentJobRepository,
    ScanJobRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.storage.default_registry import build_storage_registry
from vault_shared.workflow_events import fire_workflow_event
from worker.celery_app import celery_app
from worker.scanner.scan_service import ScannerService
from worker.tasks.enrichment import run_enrichment
from worker.tasks.storage_intelligence import run_storage_intelligence

logger = get_logger("worker.tasks.scan")

# Transient Google API failures (rate limits, network blips) are retried with
# backoff rather than failing the job outright; a scan legitimately takes
# minutes, so a handful of retries over a few minutes is still cheap relative
# to re-running the whole scan from scratch.
_MAX_RETRIES = 5
_RETRY_BACKOFF_SECONDS = 30


@celery_app.task(
    name="worker.scan.run",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_BACKOFF_SECONDS,
)
def run_scan(self: Task, scan_job_id: str) -> None:
    session = get_session_factory()()
    try:
        service = ScannerService(
            session,
            storage=build_storage_registry(
                session, oauth_client=get_google_workspace_oauth_client()
            ),
        )
        service.run(uuid.UUID(scan_job_id))
        _enqueue_enrichment_if_scan_completed(session, scan_job_id)
        _enqueue_storage_intelligence_if_scan_completed(session, scan_job_id)
        _fire_scan_completed_event(session, scan_job_id)
    except DependencyUnavailableError as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "scan_task_retries_exhausted",
                extra={"scan_job_id": scan_job_id, "attempt": self.request.retries},
            )
            jobs = ScanJobRepository(session)
            job = jobs.get_by_id(uuid.UUID(scan_job_id))
            if job is not None:
                jobs.mark_failed(job, error=f"Google Drive unavailable after retries: {exc}")
                session.commit()
            return
        logger.warning(
            "scan_task_retrying_after_dependency_error",
            extra={"scan_job_id": scan_job_id, "attempt": self.request.retries},
        )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


def _enqueue_enrichment_if_scan_completed(session: Session, scan_job_id: str) -> None:
    """The Handbook §7 pipeline handoff ("Scanner detects → Metadata
    extracted... via a scan-completed event") — implemented as a direct
    chain rather than a separate event bus, since both tasks already live
    in the same Celery app. Deliberately lives here (the orchestration
    boundary), not inside `ScannerService`, which has no reason to know the
    Metadata Engine exists at all (Handbook §8's module-boundary contract).
    Skipped for a cancelled/failed scan — only a genuinely completed scan
    has new-or-updated inventory worth enriching."""
    scan_jobs = ScanJobRepository(session)
    scan_job = scan_jobs.get_by_id(uuid.UUID(scan_job_id))
    if scan_job is None or scan_job.status != ScanStatus.COMPLETED:
        return

    enrichment_jobs = EnrichmentJobRepository(session)
    if enrichment_jobs.has_active_job(scan_job.connector_id):
        return

    enrichment_job = enrichment_jobs.create(
        connector_id=scan_job.connector_id,
        triggered_by=EnrichmentTrigger.SCAN_COMPLETED,
        triggered_by_user_id=None,
        scan_job_id=scan_job.id,
    )
    session.commit()
    run_enrichment.delay(str(enrichment_job.id))


def _enqueue_storage_intelligence_if_scan_completed(session: Session, scan_job_id: str) -> None:
    """Storage Intelligence (Phase 1) chains directly off scan completion —
    a deliberate sibling of the enrichment chain above, not a successor of
    it. Duplicate/storage-breakdown detection reads `File.checksum` and
    other raw scan fields directly (see `DuplicateDetector`), never
    Phase 5's enrichment-owned `duplicate_group_key`, so it has no real
    dependency on enrichment/embedding/recommendation having run and
    shouldn't wait behind them. `StorageAnalysisJob` is organization-
    scoped (like `RecommendationJob`), not connector-scoped, so the
    organization has to be resolved through the connector first — same
    resolution `_fire_scan_completed_event` below already does."""
    scan_jobs = ScanJobRepository(session)
    scan_job = scan_jobs.get_by_id(uuid.UUID(scan_job_id))
    if scan_job is None or scan_job.status != ScanStatus.COMPLETED:
        return

    connector = StorageConnectorRepository(session).get_by_id(scan_job.connector_id)
    if connector is None:
        return

    storage_analysis_jobs = StorageAnalysisJobRepository(session)
    if storage_analysis_jobs.has_active_job(connector.organization_id):
        return

    analysis_job = storage_analysis_jobs.create(
        organization_id=connector.organization_id,
        triggered_by=StorageAnalysisTrigger.SCAN_COMPLETED,
        triggered_by_user_id=None,
    )
    session.commit()
    run_storage_intelligence.delay(str(analysis_job.id))


def _fire_scan_completed_event(session: Session, scan_job_id: str) -> None:
    """Phase 9's `scan_completed` event trigger. `ScanJob` is connector-
    scoped, not organization-scoped (unlike `RecommendationJob`), so the
    organization has to be resolved through the connector first — same
    resolution `worker/tasks/embedding.py`'s recommendation-chaining hook
    already does for the same reason."""
    scan_jobs = ScanJobRepository(session)
    scan_job = scan_jobs.get_by_id(uuid.UUID(scan_job_id))
    if scan_job is None or scan_job.status != ScanStatus.COMPLETED:
        return
    connector = StorageConnectorRepository(session).get_by_id(scan_job.connector_id)
    if connector is None:
        return

    def _enqueue(workflow_execution_id: uuid.UUID) -> None:
        celery_app.send_task("worker.workflow.run", args=[str(workflow_execution_id)])

    fire_workflow_event(
        session,
        organization_id=connector.organization_id,
        event_type=WorkflowEventType.SCAN_COMPLETED,
        payload={"scan_job_id": str(scan_job.id)},
        enqueue_workflow_execution=_enqueue,
    )
