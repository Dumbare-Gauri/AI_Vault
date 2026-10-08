import uuid

from fastapi import APIRouter, Depends, Query

from app.application.organization_entity_service import OrganizationEntityService
from app.application.organization_recommendation_service import OrganizationRecommendationService
from app.presentation.api.v1.schemas import (
    OrganizationAnalysisJobResponse,
    OrganizationEntityDetailResponse,
    OrganizationEntityListResponse,
    OrganizationEntityResponse,
    OrganizationRecommendationListResponse,
    OrganizationRecommendationResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.services import (
    get_organization_entity_service,
    get_organization_recommendation_service,
)
from vault_shared.db.models import RoleName, User

organization_router = APIRouter(tags=["organization"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)


# Under /organization, not /intelligence: the existing
# `GET /intelligence/{intelligence_job_id}` route is registered first and
# would capture `/intelligence/entities` as a (malformed) job id.
@organization_router.get("/organization/entities", response_model=OrganizationEntityListResponse)
def list_organization_entities(
    entity_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    service: OrganizationEntityService = Depends(get_organization_entity_service),
) -> OrganizationEntityListResponse:
    entities = service.list_for_organization(user.organization_id, entity_type=entity_type)
    return OrganizationEntityListResponse(
        items=[OrganizationEntityResponse.from_model(entity) for entity in entities]
    )


@organization_router.get(
    "/organization/entities/{entity_id}", response_model=OrganizationEntityDetailResponse
)
def get_organization_entity(
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: OrganizationEntityService = Depends(get_organization_entity_service),
) -> OrganizationEntityDetailResponse:
    detail = service.get_detail(entity_id, organization_id=user.organization_id)
    return OrganizationEntityDetailResponse.from_detail(detail)


@organization_router.post(
    "/organization/analyze", response_model=OrganizationAnalysisJobResponse, status_code=201
)
def analyze_organization(
    user: User = Depends(_require_owner_or_admin),
    service: OrganizationRecommendationService = Depends(get_organization_recommendation_service),
) -> OrganizationAnalysisJobResponse:
    job = service.trigger_analysis(organization_id=user.organization_id, user_id=user.id)
    return OrganizationAnalysisJobResponse.from_model(job)


@organization_router.get(
    "/organization-recommendations", response_model=OrganizationRecommendationListResponse
)
def list_organization_recommendations(
    status: str | None = Query(default="active"),
    kind: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    service: OrganizationRecommendationService = Depends(get_organization_recommendation_service),
) -> OrganizationRecommendationListResponse:
    recommendations = service.list_for_organization(user.organization_id, status=status, kind=kind)
    return OrganizationRecommendationListResponse(
        items=[OrganizationRecommendationResponse.from_model(r) for r in recommendations]
    )


@organization_router.get(
    "/organization-recommendations/{recommendation_id}",
    response_model=OrganizationRecommendationResponse,
)
def get_organization_recommendation(
    recommendation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: OrganizationRecommendationService = Depends(get_organization_recommendation_service),
) -> OrganizationRecommendationResponse:
    recommendation = service.get_owned(recommendation_id, organization_id=user.organization_id)
    return OrganizationRecommendationResponse.from_model(recommendation)


@organization_router.post(
    "/organization-recommendations/{recommendation_id}/apply",
    response_model=OrganizationRecommendationResponse,
)
def apply_organization_recommendation(
    recommendation_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: OrganizationRecommendationService = Depends(get_organization_recommendation_service),
) -> OrganizationRecommendationResponse:
    recommendation = service.trigger_apply(
        recommendation_id, organization_id=user.organization_id, user_id=user.id
    )
    return OrganizationRecommendationResponse.from_model(recommendation)
