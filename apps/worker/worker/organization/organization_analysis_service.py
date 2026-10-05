import uuid

from sqlalchemy.orm import Session

from vault_shared import AIUnavailableError, DependencyUnavailableError, get_logger
from vault_shared.ai_gateway import AIGateway
from vault_shared.db.models import OrganizationAnalysisJob
from vault_shared.db.repositories import (
    FileRelationshipRepository,
    FileRepository,
    OrganizationAnalysisJobRepository,
)
from worker.intelligence.entity_inference_service import EntityInferenceService
from worker.organization.entity_clustering import build_clusters
from worker.organization.lifecycle_service import LifecycleService
from worker.organization.organization_recommendation_generator import (
    OrganizationRecommendationGenerator,
)

logger = get_logger("worker.organization.organization_analysis_service")


class OrganizationAnalysisService:
    """The "Analyze Organization" pass (spec section 16's "Organize my
    Drive" workflow) — entity clustering + AI-backed entity inference,
    then purely deterministic lifecycle scoring, then purely deterministic
    recommendation generation, in that order: lifecycle scoring needs this
    run's own freshly-written `FileEntityLink` rows, and recommendation
    generation needs the `OrganizationEntity` rows entity inference just
    created or confirmed. User-triggered only (`POST /v1/organization/
    analyze`), never auto-chained off enrichment/embedding completion —
    see this job's own model docstring."""

    def __init__(self, db: Session, *, ai_gateway: AIGateway) -> None:
        self._db = db
        self._jobs = OrganizationAnalysisJobRepository(db)
        self._files = FileRepository(db)
        self._relationships = FileRelationshipRepository(db)
        self._entity_inference = EntityInferenceService(db, ai_gateway=ai_gateway)
        self._lifecycle_service = LifecycleService(db)
        self._recommendation_generator = OrganizationRecommendationGenerator(db)

    def run(self, organization_analysis_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(organization_analysis_job_id)
        if job is None:
            logger.warning(
                "organization_analysis_job_not_found",
                extra={"organization_analysis_job_id": str(organization_analysis_job_id)},
            )
            return

        self._jobs.mark_running(job)
        self._db.commit()

        try:
            clusters_found, entities_created = self._run_entity_inference(job)
            self._lifecycle_service.run_for_organization(job.organization_id)
            recommendations_generated = self._recommendation_generator.generate_for_organization(
                job.organization_id
            )
        except AIUnavailableError as exc:
            if exc.retryable:
                logger.warning(
                    "organization_analysis_dependency_unavailable",
                    extra={"organization_analysis_job_id": str(job.id), "reason": exc.reason},
                )
                self._db.rollback()
                raise
            logger.warning(
                "organization_analysis_failed_non_retryable",
                extra={"organization_analysis_job_id": str(job.id), "reason": exc.reason},
            )
            self._db.rollback()
            self._jobs.mark_failed(job, error=f"AI provider error: {exc.reason}")
            self._db.commit()
            return
        except DependencyUnavailableError:
            logger.warning(
                "organization_analysis_dependency_unavailable",
                extra={"organization_analysis_job_id": str(job.id)},
            )
            self._db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception(
                "organization_analysis_failed",
                extra={"organization_analysis_job_id": str(job.id)},
            )
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._db.commit()
            return

        self._jobs.mark_completed(
            job,
            clusters_found=clusters_found,
            entities_created=entities_created,
            recommendations_generated=recommendations_generated,
        )
        self._db.commit()

    def _run_entity_inference(self, job: OrganizationAnalysisJob) -> tuple[int, int]:
        self._entity_inference.bind_org_completion_provider(job.organization_id)
        if self._entity_inference.is_stub_provider:
            # No completion provider configured — entity inference simply
            # contributes nothing this run (every file stays unlinked,
            # "Unknown is always allowed"); lifecycle scoring and
            # recommendation generation below are unaffected since neither
            # calls AI. Matches the spec's AI-outage requirement: this
            # whole job still completes with GLM disabled.
            logger.info(
                "organization_analysis_no_completion_provider",
                extra={"organization_id": str(job.organization_id)},
            )
            return 0, 0

        files = self._files.list_signal_bearing_for_organization(job.organization_id)
        edges = [
            (relationship.file_id, relationship.related_file_id)
            for relationship in self._relationships.list_for_organization(job.organization_id)
        ]
        clusters = build_clusters(files, edges)
        _clusters_processed, files_linked = self._entity_inference.infer_for_clusters(
            job.organization_id, clusters
        )
        return len(clusters), files_linked
