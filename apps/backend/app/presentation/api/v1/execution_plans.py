import uuid

from fastapi import APIRouter, Depends, Query

from app.application.execution_job_service import ExecutionJobService
from app.application.execution_plan_service import ExecutionPlanService
from app.presentation.api.v1.schemas import (
    CreateExecutionPlanRequest,
    ExecutionJobResponse,
    ExecutionPlanDetailResponse,
    ExecutionPlanResponse,
    PermanentDeleteRequest,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import (
    get_execution_job_service,
    get_execution_plan_service,
)
from app.presentation.dependencies.workspace import get_workspace
from vault_shared import ValidationError
from vault_shared.db.models import RoleName, StorageConnector, User

execution_plans_router = APIRouter(tags=["execution-plans"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)
_create_plan_rate_limit = rate_limiter("execution-plan-create", limit=30, window_seconds=60)


@execution_plans_router.post(
    "/execution-plans",
    response_model=ExecutionPlanResponse,
    status_code=201,
    dependencies=[Depends(_create_plan_rate_limit)],
)
def create_execution_plan(
    request: CreateExecutionPlanRequest,
    workspace: StorageConnector = Depends(get_workspace),
    user: User = Depends(_require_owner_or_admin),
    service: ExecutionPlanService = Depends(get_execution_plan_service),
) -> ExecutionPlanResponse:
    """Runs immediately (ADR-026) — no approval step exists for a
    directly-triggered action. `require_approval=False` marks the plan
    approved and enqueues its job in the same call that creates it; a
    failure to enqueue (e.g. the connector lacks write scope) surfaces as
    this request's own error rather than a plan silently left waiting for
    a review nobody will ever perform."""
    has_recommendation = request.recommendation_id is not None
    has_duplicate_group = request.duplicate_group_id is not None
    has_ad_hoc = bool(request.file_ids or request.folder_ids)
    if sum([has_recommendation, has_duplicate_group, has_ad_hoc]) != 1:
        raise ValidationError(
            "Provide exactly one of recommendation_id, duplicate_group_id, "
            "or file_ids (with action_type)."
        )

    if has_recommendation:
        plan = service.create_plan(
            uuid.UUID(request.recommendation_id),
            organization_id=user.organization_id,
            user_id=user.id,
            require_approval=False,
            connector_id=workspace.id,
        )
    elif has_duplicate_group:
        plan = service.create_plan_from_duplicate_group(
            uuid.UUID(request.duplicate_group_id),
            organization_id=user.organization_id,
            user_id=user.id,
            require_approval=False,
            connector_id=workspace.id,
        )
    else:
        if not request.action_type:
            raise ValidationError("action_type is required when providing file_ids.")
        plan = service.create_ad_hoc_plan(
            [uuid.UUID(fid) for fid in request.file_ids or []],
            action_type=request.action_type,
            organization_id=user.organization_id,
            user_id=user.id,
            new_name=request.new_name,
            new_parent_id=request.new_parent_id,
            remove_originals=request.remove_originals,
            folder_ids=[uuid.UUID(fid) for fid in request.folder_ids or []],
            archive_name=request.archive_name,
            archive_folder_id=request.archive_folder_id,
            require_approval=False,
            connector_id=workspace.id,
            allow_protected=request.include_protected,
        )
    return ExecutionPlanResponse.from_model(plan)


@execution_plans_router.post(
    "/execution-plans/permanent-delete",
    response_model=ExecutionPlanResponse,
    status_code=201,
    dependencies=[Depends(_create_plan_rate_limit)],
)
def create_permanent_delete_plan(
    request: PermanentDeleteRequest,
    workspace: StorageConnector = Depends(get_workspace),
    user: User = Depends(_require_owner_or_admin),
    service: ExecutionPlanService = Depends(get_execution_plan_service),
) -> ExecutionPlanResponse:
    """Runs immediately once confirmed. The confirmation is checked here,
    on the server, not only in the dialog: the request must carry the exact
    phrase naming how many files are deleted."""
    if not request.file_ids:
        raise ValidationError("Select at least one file.")
    plan = service.create_permanent_delete_plan(
        [uuid.UUID(fid) for fid in request.file_ids],
        organization_id=user.organization_id,
        user_id=user.id,
        confirmation=request.confirmation,
        require_approval=False,
        allow_protected=request.include_protected,
        connector_id=workspace.id,
    )
    return ExecutionPlanResponse.from_model(plan)


@execution_plans_router.get("/execution-plans", response_model=list[ExecutionPlanResponse])
def list_execution_plans(
    status: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    service: ExecutionPlanService = Depends(get_execution_plan_service),
) -> list[ExecutionPlanResponse]:
    plans = service.list_for_organization(user.organization_id, status=status)
    return [ExecutionPlanResponse.from_model(plan) for plan in plans]


@execution_plans_router.get(
    "/execution-plans/{execution_plan_id}", response_model=ExecutionPlanDetailResponse
)
def get_execution_plan(
    execution_plan_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ExecutionPlanService = Depends(get_execution_plan_service),
) -> ExecutionPlanDetailResponse:
    detail = service.get_detail(execution_plan_id, organization_id=user.organization_id)
    return ExecutionPlanDetailResponse.from_detail(detail)


@execution_plans_router.post(
    "/execution-plans/{execution_plan_id}/rollback",
    response_model=ExecutionJobResponse,
    status_code=201,
)
def rollback_execution_plan(
    execution_plan_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: ExecutionJobService = Depends(get_execution_job_service),
) -> ExecutionJobResponse:
    job = service.trigger_rollback(
        execution_plan_id, organization_id=user.organization_id, user_id=user.id
    )
    return ExecutionJobResponse.from_model(job)
