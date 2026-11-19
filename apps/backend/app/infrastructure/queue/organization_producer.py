from uuid import UUID

from app.infrastructure.queue.celery_client import get_celery_client
from app.infrastructure.queue.correlation import correlation_headers

# Must match the task names registered by apps/worker/worker/tasks/
# organization.py and worker/tasks/execution.py respectively.
_ANALYZE_TASK_NAME = "worker.organization.run_analysis"
_APPLY_TASK_NAME = "worker.organization.apply_recommendation"


def enqueue_organization_analysis_job(organization_analysis_job_id: UUID) -> None:
    get_celery_client().send_task(
        _ANALYZE_TASK_NAME,
        args=[str(organization_analysis_job_id)],
        headers=correlation_headers(),
    )


def enqueue_organization_recommendation_apply(
    recommendation_id: UUID, *, organization_id: UUID, user_id: UUID
) -> None:
    get_celery_client().send_task(
        _APPLY_TASK_NAME,
        args=[str(recommendation_id)],
        kwargs={"organization_id": str(organization_id), "user_id": str(user_id)},
        headers=correlation_headers(),
    )
