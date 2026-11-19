import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import OrganizationAnalysisJob, OrganizationAnalysisJobStatus


class OrganizationAnalysisJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, organization_id: uuid.UUID, triggered_by_user_id: uuid.UUID | None
    ) -> OrganizationAnalysisJob:
        job = OrganizationAnalysisJob(
            organization_id=organization_id, triggered_by_user_id=triggered_by_user_id
        )
        self._session.add(job)
        self._session.flush()
        return job

    def get_by_id(self, job_id: uuid.UUID) -> OrganizationAnalysisJob | None:
        return self._session.get(OrganizationAnalysisJob, job_id)

    def get_owned(
        self, job_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> OrganizationAnalysisJob | None:
        return (
            self._session.query(OrganizationAnalysisJob)
            .filter_by(id=job_id, organization_id=organization_id)
            .first()
        )

    def has_active_job(self, organization_id: uuid.UUID) -> bool:
        return (
            self._session.query(OrganizationAnalysisJob)
            .filter(
                OrganizationAnalysisJob.organization_id == organization_id,
                OrganizationAnalysisJob.status.in_(
                    [OrganizationAnalysisJobStatus.PENDING, OrganizationAnalysisJobStatus.RUNNING]
                ),
            )
            .first()
            is not None
        )

    def mark_running(self, job: OrganizationAnalysisJob) -> None:
        job.status = OrganizationAnalysisJobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        self._session.flush()

    def mark_completed(
        self,
        job: OrganizationAnalysisJob,
        *,
        clusters_found: int,
        entities_created: int,
        recommendations_generated: int,
    ) -> None:
        job.status = OrganizationAnalysisJobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)
        job.clusters_found = clusters_found
        job.entities_created = entities_created
        job.recommendations_generated = recommendations_generated
        self._session.flush()

    def mark_failed(self, job: OrganizationAnalysisJob, *, error: str) -> None:
        job.status = OrganizationAnalysisJobStatus.FAILED
        job.error = error[:2048]
        job.completed_at = datetime.now(UTC)
        self._session.flush()

    def list_for_organization(self, organization_id: uuid.UUID) -> list[OrganizationAnalysisJob]:
        return (
            self._session.query(OrganizationAnalysisJob)
            .filter_by(organization_id=organization_id)
            .order_by(OrganizationAnalysisJob.created_at.desc())
            .all()
        )
