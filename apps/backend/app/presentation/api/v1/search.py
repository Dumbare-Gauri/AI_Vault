from fastapi import APIRouter, Depends

from app.application.search_service import SearchService
from app.presentation.api.v1.schemas import SearchRequest, SearchResponse, SearchResultResponse
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import get_search_service
from vault_shared.db.models import User
from vault_shared.search import FileQuery

search_router = APIRouter(tags=["search"])

# Phase 10 (ADR-022) — every AI-Gateway-backed endpoint gets a rate limit;
# search embeds the query on every call, the same cost profile as an
# embedding job, just synchronous and per-request instead of batched.
_search_rate_limit = rate_limiter("search", limit=60, window_seconds=60)


@search_router.post(
    "/search", response_model=SearchResponse, dependencies=[Depends(_search_rate_limit)]
)
def search(
    request: SearchRequest,
    user: User = Depends(get_current_user),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    """Plain words ("python files larger than 500 MB before 2023") and/or
    explicit filters. Every result is a real, indexed, non-trashed file."""
    f = request.filters
    filters = FileQuery(
        categories=tuple(f.categories) if f else (),
        extensions=tuple(e.lower().lstrip(".") for e in f.extensions) if f else (),
        size_min=f.size_min if f else None,
        size_max=f.size_max if f else None,
        modified_after=f.modified_after if f else None,
        modified_before=f.modified_before if f else None,
        folder=f.folder if f else None,
        connector_id=f.connector_id if f else None,
        ownership=f.ownership if f else None,
        sort=f.sort if f else "relevance",
        limit=request.limit,
        offset=request.offset,
    )
    outcome = service.find(
        request.query,
        organization_id=user.organization_id,
        user_id=user.id,
        filters=filters,
    )
    return SearchResponse(
        query=request.query,
        total=outcome.total,
        understood=outcome.understood,
        interpreted_by_ai=outcome.interpreted_by_ai,
        results=[SearchResultResponse.from_result(r) for r in outcome.results],
    )
