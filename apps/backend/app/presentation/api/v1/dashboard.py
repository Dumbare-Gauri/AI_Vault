from fastapi import APIRouter, Depends, Response

from app.application.dashboard_service import DashboardService
from app.infrastructure.cache.response_cache import get_cached_json, set_cached_json
from app.presentation.api.v1.schemas import DashboardResponse
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_dashboard_service
from app.presentation.dependencies.workspace import get_workspace
from vault_shared import get_settings
from vault_shared.db.models import StorageConnector, User

dashboard_router = APIRouter(tags=["dashboard"])


@dashboard_router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(
    workspace: StorageConnector = Depends(get_workspace),
    user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
) -> Response:
    # Phase 10 (ADR-022) — cached per organization *and* storage workspace:
    # each is a different query result, and sharing a cache entry would leak
    # one org's (or one storage's) data into another's response.
    cache_key = f"dashboard:{user.organization_id}:{workspace.id}"
    cached = get_cached_json(cache_key)
    if cached is not None:
        return Response(content=cached, media_type="application/json")

    overview = service.get_overview(user.organization_id, connector_id=workspace.id)
    body = DashboardResponse.from_overview(overview).model_dump_json()
    set_cached_json(cache_key, body, ttl_seconds=get_settings().dashboard_cache_ttl_seconds)
    return Response(content=body, media_type="application/json")
