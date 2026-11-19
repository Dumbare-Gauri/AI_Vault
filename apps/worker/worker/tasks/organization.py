import uuid

from celery import Task

from vault_shared import DependencyUnavailableError, get_logger
from vault_shared.ai_gateway import get_ai_gateway
from vault_shared.db.repositories import OrganizationAnalysisJobRepository
from vault_shared.db.session import get_session_factory
from worker.celery_app import celery_app
from worker.organization.organization_analysis_service import OrganizationAnalysisService

logger = get_logger("worker.tasks.organization")

# Same retry policy as worker.tasks.intelligence.run_intelligence — this
# job's entity-inference step calls the same hosted LLM and so has the
# same external-connectivity failure mode.
_MAX_RETRIES = 5
_RETRY_BACKOFF_SECONDS = 30


@celery_app.task(
    name="worker.organization.run_analysis",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_BACKOFF_SECONDS,
)
def run_organization_analysis(self: Task, organization_analysis_job_id: str) -> None:
    session = get_session_factory()()
    try:
        service = OrganizationAnalysisService(session, ai_gateway=get_ai_gateway())
        service.run(uuid.UUID(organization_analysis_job_id))
    except DependencyUnavailableError as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "organization_analysis_task_retries_exhausted",
                extra={
                    "organization_analysis_job_id": organization_analysis_job_id,
                    "attempt": self.request.retries,
                },
            )
            jobs = OrganizationAnalysisJobRepository(session)
            job = jobs.get_by_id(uuid.UUID(organization_analysis_job_id))
            if job is not None:
                jobs.mark_failed(job, error=f"AI provider unavailable after retries: {exc}")
                session.commit()
            return
        logger.warning(
            "organization_analysis_task_retrying_after_dependency_error",
            extra={
                "organization_analysis_job_id": organization_analysis_job_id,
                "attempt": self.request.retries,
            },
        )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
