from fastapi import APIRouter, Depends

from app.application.ai_provider_config_service import AIProviderConfigService
from app.application.organization_service import OrganizationService
from app.presentation.api.v1.schemas import (
    AIProviderConfigResponse,
    AIProviderConfigTestRequest,
    AIProviderConfigTestResponse,
    AIProviderConfigUpdateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import (
    get_ai_provider_config_service,
    get_organization_service,
)
from vault_shared.db.models import RoleName, User

organizations_router = APIRouter(prefix="/organizations", tags=["organizations"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)
_ai_provider_test_rate_limit = rate_limiter("ai-provider-test", limit=5, window_seconds=60)


@organizations_router.get("/current", response_model=OrganizationResponse)
def get_current_organization(user: User = Depends(get_current_user)) -> OrganizationResponse:
    return OrganizationResponse.from_model(user.organization)


@organizations_router.patch("/current", response_model=OrganizationResponse)
def update_current_organization(
    body: OrganizationUpdateRequest,
    user: User = Depends(_require_owner_or_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationResponse:
    organization = service.rename(user.organization, name=body.name)
    return OrganizationResponse.from_model(organization)


@organizations_router.get("/current/ai-provider", response_model=AIProviderConfigResponse)
def get_ai_provider_config(
    user: User = Depends(get_current_user),
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
) -> AIProviderConfigResponse:
    config = service.get_status(user.organization_id)
    return AIProviderConfigResponse.from_model(config)


@organizations_router.put("/current/ai-provider", response_model=AIProviderConfigResponse)
def set_ai_provider_config(
    body: AIProviderConfigUpdateRequest,
    user: User = Depends(_require_owner_or_admin),
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
) -> AIProviderConfigResponse:
    config = service.set(
        user.organization_id,
        api_key=body.api_key,
        model_name=body.model_name,
        provider=body.provider,
    )
    return AIProviderConfigResponse.from_model(config)


@organizations_router.delete("/current/ai-provider", status_code=204)
def clear_ai_provider_config(
    user: User = Depends(_require_owner_or_admin),
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
) -> None:
    service.clear(user.organization_id)


@organizations_router.post(
    "/current/ai-provider/test",
    response_model=AIProviderConfigTestResponse,
    dependencies=[Depends(_ai_provider_test_rate_limit)],
)
def test_ai_provider_config(
    body: AIProviderConfigTestRequest,
    user: User = Depends(_require_owner_or_admin),
    service: AIProviderConfigService = Depends(get_ai_provider_config_service),
) -> AIProviderConfigTestResponse:
    success, error = service.test(
        user.organization_id,
        api_key=body.api_key,
        model_name=body.model_name,
        provider=body.provider,
    )
    return AIProviderConfigTestResponse(success=success, error=error)
