import uuid

from celery import Task
from sqlalchemy.orm import Session

from vault_shared import DependencyUnavailableError, get_logger
from vault_shared.connectors.google_workspace import get_google_workspace_oauth_client
from vault_shared.db.models import (
    EmbeddingTrigger,
    EnrichmentJobStatus,
    IntelligenceTrigger,
    WorkflowEventType,
)
from vault_shared.db.repositories import (
    EmbeddingJobRepository,
    EnrichmentJobRepository,
    IntelligenceJobRepository,
    StorageConnectorRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.storage.default_registry import build_storage_registry
from vault_shared.workflow_events import fire_workflow_event
from worker.celery_app import celery_app
from worker.enrichment.enrichment_service import EnrichmentService
from worker.tasks.embedding import run_embedding
from worker.tasks.intelligence import run_intelligence

logger = get_logger("worker.tasks.enrichment")

# Same retry policy as worker.scan.run (ADR-016) — a Drive-connectivity or
# token failure affects every remaining file identically, so the whole job
# is retried rather than any single file.
_MAX_RETRIES = 5
_RETRY_BACKOFF_SECONDS = 30


@celery_app.task(
    name="worker.enrichment.run",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_BACKOFF_SECONDS,
)
def run_enrichment(self: Task, enrichment_job_id: str) -> None:
    session = get_session_factory()()
    try:
        service = EnrichmentService(
            session,
            storage=build_storage_registry(
                session, oauth_client=get_google_workspace_oauth_client()
            ),
        )
        service.run(uuid.UUID(enrichment_job_id))
        _enqueue_embedding_if_enrichment_completed(session, enrichment_job_id)
        _enqueue_intelligence_if_enrichment_completed(session, enrichment_job_id)
        _fire_enrichment_completed_event(session, enrichment_job_id)
    except DependencyUnavailableError as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "enrichment_task_retries_exhausted",
                extra={"enrichment_job_id": enrichment_job_id, "attempt": self.request.retries},
            )
            jobs = EnrichmentJobRepository(session)
            job = jobs.get_by_id(uuid.UUID(enrichment_job_id))
            if job is not None:
                jobs.mark_failed(job, error=f"Google Drive unavailable after retries: {exc}")
                session.commit()
            return
        logger.warning(
            "enrichment_task_retrying_after_dependency_error",
            extra={"enrichment_job_id": enrichment_job_id, "attempt": self.request.retries},
        )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


def _enqueue_embedding_if_enrichment_completed(session: Session, enrichment_job_id: str) -> None:
    """Handbook §7/Phase 6's Architecture Impact: Knowledge Engine →
    Embedding Engine. Mirrors `worker.tasks.scan._enqueue_enrichment_if_scan_completed`
    exactly — lives at this orchestration boundary, not inside
    `EnrichmentService`, which has no reason to know the Embedding Engine
    exists. Skipped for a cancelled/failed enrichment, or if an embedding
    job is already active for the connector."""
    enrichment_jobs = EnrichmentJobRepository(session)
    enrichment_job = enrichment_jobs.get_by_id(uuid.UUID(enrichment_job_id))
    if enrichment_job is None or enrichment_job.status != EnrichmentJobStatus.COMPLETED:
        return

    embedding_jobs = EmbeddingJobRepository(session)
    if embedding_jobs.has_active_job(enrichment_job.connector_id):
        return

    embedding_job = embedding_jobs.create(
        connector_id=enrichment_job.connector_id,
        triggered_by=EmbeddingTrigger.ENRICHMENT_COMPLETED,
        triggered_by_user_id=None,
        enrichment_job_id=enrichment_job.id,
    )
    session.commit()
    run_embedding.delay(str(embedding_job.id))


def _enqueue_intelligence_if_enrichment_completed(session: Session, enrichment_job_id: str) -> None:
    """Phase 2's AI File Intelligence pipeline — triggered in *parallel*
    with `_enqueue_embedding_if_enrichment_completed` above, off the same
    `EnrichmentJob.status == COMPLETED` event, not sequentially after it.
    `EmbeddingService` has no external-network failure mode (its provider
    is fully local); `IntelligenceService` calls a real hosted LLM and
    does, so a slow/unreachable/unconfigured completion provider must
    never delay Embedding's search-readiness path — see
    `IntelligenceJob`'s own docstring for the full reasoning and the
    documented, non-blocking race this creates with `RecommendationJob`
    (which triggers off Embedding completion, not this job)."""
    enrichment_jobs = EnrichmentJobRepository(session)
    enrichment_job = enrichment_jobs.get_by_id(uuid.UUID(enrichment_job_id))
    if enrichment_job is None or enrichment_job.status != EnrichmentJobStatus.COMPLETED:
        return

    intelligence_jobs = IntelligenceJobRepository(session)
    if intelligence_jobs.has_active_job(enrichment_job.connector_id):
        return

    intelligence_job = intelligence_jobs.create(
        connector_id=enrichment_job.connector_id,
        triggered_by=IntelligenceTrigger.ENRICHMENT_COMPLETED,
        triggered_by_user_id=None,
        enrichment_job_id=enrichment_job.id,
    )
    session.commit()
    run_intelligence.delay(str(intelligence_job.id))


def _fire_enrichment_completed_event(session: Session, enrichment_job_id: str) -> None:
    """Phase 9's `enrichment_completed` event trigger — same connector→
    organization resolution as `worker.tasks.scan._fire_scan_completed_event`."""
    enrichment_jobs = EnrichmentJobRepository(session)
    enrichment_job = enrichment_jobs.get_by_id(uuid.UUID(enrichment_job_id))
    if enrichment_job is None or enrichment_job.status != EnrichmentJobStatus.COMPLETED:
        return
    connector = StorageConnectorRepository(session).get_by_id(enrichment_job.connector_id)
    if connector is None:
        return

    def _enqueue(workflow_execution_id: uuid.UUID) -> None:
        celery_app.send_task("worker.workflow.run", args=[str(workflow_execution_id)])

    fire_workflow_event(
        session,
        organization_id=connector.organization_id,
        event_type=WorkflowEventType.ENRICHMENT_COMPLETED,
        payload={"enrichment_job_id": str(enrichment_job.id)},
        enqueue_workflow_execution=_enqueue,
    )
