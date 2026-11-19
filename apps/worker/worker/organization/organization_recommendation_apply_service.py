import uuid

from sqlalchemy.orm import Session

from vault_shared import ConflictError, NotFoundError, ValidationError, get_logger
from vault_shared.db.models import (
    ExecutionActionType,
    Folder,
    OrganizationRecommendationKind,
    OrganizationRecommendationStatus,
)
from vault_shared.db.repositories import (
    FileRepository,
    FolderRepository,
    OrganizationRecommendationRepository,
)
from vault_shared.execution.plan_service import ExecutionPlanService
from worker.execution.execution_service import ExecutionService

logger = get_logger("worker.organization.organization_recommendation_apply_service")


def _enqueue_execution_job(execution_job_id: uuid.UUID) -> None:
    # Same worker-side enqueue pattern `worker.workflow.execution_service.
    # ApprovalService._enqueue_execution_job` already uses — this app's own
    # Celery app, never a cross-app import (apps/backend injects its own
    # producer function into the same shared `ExecutionPlanService`/
    # `ApprovalService` classes instead).
    from worker.celery_app import celery_app

    celery_app.send_task("worker.execution.run", args=[str(execution_job_id)])


class OrganizationRecommendationApplyService:
    """Bridges an `OrganizationRecommendation` to the real Execution
    Engine (spec: "Recommendation -> Validation -> Policy -> Confirmation
    -> Execution -> GoogleDriveAdapter -> Google Drive"). The folder-
    creation step calls `ExecutionService.create_folder()` directly — see
    that method's own docstring for why this is deliberately outside the
    `ExecutionStep` machinery. The move step reuses the existing,
    completely unchanged `ExecutionPlanService.create_ad_hoc_plan(
    action_type=MOVE_FILE, ...)` ad-hoc path with `require_approval=False`
    (ADR-026): the user's "Apply" click on this recommendation already is
    their confirmation, so nothing here waits on a second approval."""

    def __init__(self, db: Session, *, execution_service: ExecutionService) -> None:
        """Takes an already-constructed `ExecutionService` rather than
        building one itself — assembling a `StorageAdapterProvider`
        (`build_storage_registry`) is restricted to a small set of
        composition roots (`test_architecture_boundaries.
        TestProviderCodeIsConfinedToAdaptersAndCompositionRoots`), and this
        service is not one; its caller (`worker.tasks.execution`) already
        is, for the plain `ExecutionService` it builds."""
        self._db = db
        self._recommendations = OrganizationRecommendationRepository(db)
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._execution_service = execution_service
        self._plan_service = ExecutionPlanService(db, enqueue_execution_job=_enqueue_execution_job)

    def apply(
        self, recommendation_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> uuid.UUID:
        """Returns the resulting `ExecutionPlan.id` — the plan's own
        status is the ground truth for whether the move actually
        succeeded; this method only gets the recommendation to APPLIED."""
        recommendation = self._recommendations.get_owned(
            recommendation_id, organization_id=organization_id
        )
        if recommendation is None:
            raise NotFoundError("Organization recommendation not found.")
        if recommendation.status != OrganizationRecommendationStatus.ACTIVE:
            raise ConflictError(f"This recommendation is already {recommendation.status}.")
        if not recommendation.suggested_destination:
            raise ValidationError("This recommendation has no suggested destination.")

        file_ids = [uuid.UUID(file_id) for file_id in recommendation.affected_file_ids]
        files = self._files.list_owned_by_organization(file_ids, organization_id=organization_id)
        if not files:
            raise ValidationError(
                "None of this recommendation's affected files could be found — it may be "
                "stale. Re-analyze and try again."
            )

        if recommendation.kind == OrganizationRecommendationKind.RENAME_FILE:
            plan = self._plan_service.create_ad_hoc_plan(
                [files[0].id],
                action_type=ExecutionActionType.RENAME,
                organization_id=organization_id,
                user_id=user_id,
                new_name=recommendation.suggested_destination[-1],
                require_approval=False,
            )
            self._recommendations.mark_applied(recommendation, execution_plan_id=plan.id)
            self._db.commit()
            return plan.id

        destination_folder = self._resolve_or_create_destination(
            organization_id=organization_id,
            storage_source_id=files[0].storage_source_id,
            path_parts=recommendation.suggested_destination,
        )

        plan = self._plan_service.create_ad_hoc_plan(
            [file.id for file in files],
            action_type=ExecutionActionType.MOVE_FILE,
            organization_id=organization_id,
            user_id=user_id,
            new_parent_id=destination_folder.provider_file_id,
            require_approval=False,
        )
        self._recommendations.mark_applied(recommendation, execution_plan_id=plan.id)
        self._db.commit()
        return plan.id

    def _resolve_or_create_destination(
        self,
        *,
        organization_id: uuid.UUID,
        storage_source_id: uuid.UUID,
        path_parts: list[str],
    ) -> Folder:
        """Walks `path_parts` (e.g. `["Projects", "Phoenix"]`) from this
        storage source's root, creating only the components that don't
        already exist. Each component is matched by name among the
        current parent's existing child folders — ambiguity (two folders
        sharing a name under the same parent, a pre-existing possibility
        this flow doesn't create) resolves to the first match, same as
        any other by-name lookup in this codebase."""
        parent: Folder | None = None
        for part in path_parts:
            existing = self._folders.get_by_parent_and_name(
                storage_source_id=storage_source_id,
                parent_folder_id=parent.id if parent else None,
                name=part,
            )
            if existing is not None:
                parent = existing
            else:
                parent = self._execution_service.create_folder(
                    organization_id=organization_id,
                    storage_source_id=storage_source_id,
                    name=part,
                    parent_folder_id=parent.id if parent else None,
                )
        assert parent is not None  # path_parts is validated non-empty by the caller
        return parent
