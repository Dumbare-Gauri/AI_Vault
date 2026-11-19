import uuid

from fastapi import APIRouter, Depends

from app.application.action_service import ActionResult, ActionService
from app.presentation.api.v1.schemas import (
    ActionArchiveResponse,
    ActionProblemResponse,
    ActionResponse,
    ActivityItemResponse,
    ActivityResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.services import get_action_service
from vault_shared.db.models import RoleName, User

actions_router = APIRouter(tags=["actions"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)


def _to_response(result: ActionResult) -> ActionResponse:
    return ActionResponse(
        id=str(result.id),
        kind=result.kind,
        status=result.status,
        message=result.message,
        total=result.total,
        succeeded=result.succeeded,
        failed=result.failed,
        verified=result.verified,
        provider=result.provider,
        created_at=result.created_at,
        can_undo=result.can_undo,
        problems=[
            ActionProblemResponse(file_name=p.file_name, reason=p.reason) for p in result.problems
        ],
        archive=(
            ActionArchiveResponse(**result.archive.__dict__) if result.archive is not None else None
        ),
    )


@actions_router.get("/actions/{action_id}", response_model=ActionResponse)
def get_action(
    action_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return _to_response(service.get(action_id, organization_id=user.organization_id))


@actions_router.post("/actions/{action_id}/undo", response_model=ActionResponse)
def undo_action(
    action_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return _to_response(
        service.undo(action_id, organization_id=user.organization_id, user_id=user.id)
    )


@actions_router.get("/activity", response_model=ActivityResponse)
def recent_activity(
    user: User = Depends(get_current_user),
    service: ActionService = Depends(get_action_service),
) -> ActivityResponse:
    return ActivityResponse(
        items=[
            ActivityItemResponse(
                id=item.id, kind=item.kind, status=item.status, message=item.message, at=item.at
            )
            for item in service.recent_activity(user.organization_id)
        ]
    )
