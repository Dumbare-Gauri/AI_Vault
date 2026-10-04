import uuid

from vault_shared.connectors.google_workspace import get_google_workspace_oauth_client
from vault_shared.db.session import get_session_factory
from vault_shared.object_storage import ObjectStorageClient
from vault_shared.settings import get_settings
from vault_shared.storage.default_registry import build_storage_registry
from worker.celery_app import celery_app
from worker.execution.execution_service import ExecutionService


@celery_app.task(
    name="worker.execution.run", soft_time_limit=get_settings().execution_timeout_seconds
)
def run_execution(execution_job_id: str) -> None:
    """No `self.retry(...)` branch, unlike `worker.enrichment.run` — a
    failed execution *step* is already isolated per-file inside
    `ExecutionService` (never raises out to here), and a whole-job
    failure (e.g. the connector losing access mid-run) is deliberately
    terminal, not silently retried against real customer storage without
    a fresh human approval — the founder decides whether to try again via
    a new plan, not this task's own retry loop. `soft_time_limit` (the
    phase spec's `EXECUTION_TIMEOUT`) still bounds one run's wall-clock
    time, and Celery's `task_acks_late`/`task_reject_on_worker_lost`
    (`celery_app.py`) still redeliver on an actual worker-process crash —
    that's a different failure mode than a domain-level execution error."""
    session = get_session_factory()()
    try:
        service = ExecutionService(
            session,
            storage=build_storage_registry(
                session, oauth_client=get_google_workspace_oauth_client()
            ),
            object_storage_client=ObjectStorageClient(),
        )
        service.run(uuid.UUID(execution_job_id))
    finally:
        session.close()
