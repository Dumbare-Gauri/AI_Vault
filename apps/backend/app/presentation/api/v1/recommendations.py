import uuid

from fastapi import APIRouter, Depends, Query

from app.application.recommendation_service import RecommendationService
from app.presentation.api.v1.schemas import (
    RecommendationJobResponse,
    RecommendationListResponse,
    RecommendationResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import get_recommendation_service
from app.presentation.dependencies.workspace import get_workspace
from vault_shared.db.models import RoleName, StorageConnector, User

recommendations_router = APIRouter(tags=["recommendations"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)
_refresh_rate_limit = rate_limiter("recommendations-refresh", limit=10, window_seconds=60)


@recommendations_router.get("/recommendations", response_model=RecommendationListResponse)
def list_recommendations(
    category: str | None = Query(default=None),
    status: str | None = Query(default="active"),
    search: str | None = Query(default=None),
    workspace: StorageConnector = Depends(get_workspace),
    user: User = Depends(get_current_user),
    service: RecommendationService = Depends(get_recommendation_service),
) -> RecommendationListResponse:
    recommendations = service.list_for_organization(
        user.organization_id,
        role=user.role.name,
        category=category,
        status=status,
        search=search,
        connector_id=workspace.id,
    )
    return RecommendationListResponse(
        items=[RecommendationResponse.from_model(r) for r in recommendations]
    )


@recommendations_router.post(
    "/recommendations/refresh",
    response_model=RecommendationJobResponse,
    status_code=201,
    dependencies=[Depends(_refresh_rate_limit)],
)
def refresh_recommendations(
    user: User = Depends(_require_owner_or_admin),
    service: RecommendationService = Depends(get_recommendation_service),
) -> RecommendationJobResponse:
    job = service.trigger_refresh(organization_id=user.organization_id, user_id=user.id)
    return RecommendationJobResponse.from_model(job)


@recommendations_router.get(
    "/recommendations/{recommendation_id}", response_model=RecommendationResponse
)
def get_recommendation(
    recommendation_id: uuid.UUID,
    workspace: StorageConnector = Depends(get_workspace),
    user: User = Depends(get_current_user),
    service: RecommendationService = Depends(get_recommendation_service),
) -> RecommendationResponse:
    recommendation = service.get_owned(
        recommendation_id,
        organization_id=user.organization_id,
        user_id=user.id,
        role=user.role.name,
        connector_id=workspace.id,
    )
    return RecommendationResponse.from_model(recommendation)
