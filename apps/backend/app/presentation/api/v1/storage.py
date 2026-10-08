import uuid

from fastapi import APIRouter, Depends, Query

from app.application.storage_intelligence_service import StorageIntelligenceService
from app.presentation.api.v1.schemas import (
    DuplicateGroupDetailResponse,
    DuplicateGroupListResponse,
    DuplicateGroupResponse,
    StorageAnalysisJobResponse,
    StorageFileListResponse,
    StorageFileResponse,
    StorageOverviewResponse,
    StorageStatisticsResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import get_storage_intelligence_service
from vault_shared.db.models import RoleName, User
from vault_shared.storage_intelligence.thresholds import (
    INACTIVE_FILE_DAYS_DEFAULT,
    LARGE_FILE_BYTES_DEFAULT,
    OLD_FILE_DAYS_DEFAULT,
)

storage_router = APIRouter(tags=["storage"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)
_analyze_rate_limit = rate_limiter("storage-analyze", limit=10, window_seconds=60)

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


@storage_router.get("/storage/overview", response_model=StorageOverviewResponse)
def get_storage_overview(
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageOverviewResponse:
    snapshot = service.get_latest_snapshot(user.organization_id, connector_id=connector_id)
    latest_job = service.get_latest_job(user.organization_id)
    return StorageOverviewResponse.from_models(snapshot, latest_job)


@storage_router.get("/storage/statistics", response_model=StorageStatisticsResponse)
def get_storage_statistics(
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageStatisticsResponse:
    snapshot = service.get_latest_snapshot(user.organization_id, connector_id=connector_id)
    return StorageStatisticsResponse.from_model(snapshot)


@storage_router.post(
    "/storage/analyze",
    response_model=StorageAnalysisJobResponse,
    status_code=201,
    dependencies=[Depends(_analyze_rate_limit)],
)
def trigger_storage_analysis(
    user: User = Depends(_require_owner_or_admin),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageAnalysisJobResponse:
    job = service.trigger_analysis(organization_id=user.organization_id, user_id=user.id)
    return StorageAnalysisJobResponse.from_model(job)


@storage_router.get("/storage/duplicates", response_model=DuplicateGroupListResponse)
def list_duplicate_groups(
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> DuplicateGroupListResponse:
    groups, total = service.list_duplicate_groups(
        user.organization_id, limit=limit, offset=offset, connector_id=connector_id
    )
    return DuplicateGroupListResponse(
        items=[DuplicateGroupResponse.from_model(g) for g in groups], total=total
    )


@storage_router.get("/storage/duplicates/{group_id}", response_model=DuplicateGroupDetailResponse)
def get_duplicate_group(
    group_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> DuplicateGroupDetailResponse:
    group, members = service.get_duplicate_group(group_id, organization_id=user.organization_id)
    return DuplicateGroupDetailResponse.from_model_with_members(group, members)


@storage_router.get("/storage/large-files", response_model=StorageFileListResponse)
def list_large_files(
    min_size_bytes: int = Query(default=LARGE_FILE_BYTES_DEFAULT, ge=0),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageFileListResponse:
    files, total = service.list_large_files(
        user.organization_id,
        min_size_bytes=min_size_bytes,
        limit=limit,
        offset=offset,
        connector_id=connector_id,
    )
    return StorageFileListResponse(
        items=[StorageFileResponse.from_model(f) for f in files], total=total
    )


@storage_router.get("/storage/old-files", response_model=StorageFileListResponse)
def list_old_files(
    older_than_days: int = Query(default=OLD_FILE_DAYS_DEFAULT, ge=1),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageFileListResponse:
    files, total = service.list_old_files(
        user.organization_id,
        older_than_days=older_than_days,
        limit=limit,
        offset=offset,
        connector_id=connector_id,
    )
    return StorageFileListResponse(
        items=[StorageFileResponse.from_model(f) for f in files], total=total
    )


@storage_router.get("/storage/inactive-files", response_model=StorageFileListResponse)
def list_inactive_files(
    inactive_days: int = Query(default=INACTIVE_FILE_DAYS_DEFAULT, ge=1),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageFileListResponse:
    files, total = service.list_inactive_files(
        user.organization_id,
        inactive_days=inactive_days,
        limit=limit,
        offset=offset,
        connector_id=connector_id,
    )
    return StorageFileListResponse(
        items=[StorageFileResponse.from_model(f) for f in files], total=total
    )


@storage_router.get("/storage/candidates", response_model=StorageFileListResponse)
def list_storage_candidates(
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    connector_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    service: StorageIntelligenceService = Depends(get_storage_intelligence_service),
) -> StorageFileListResponse:
    files, total = service.list_candidates(
        user.organization_id, limit=limit, offset=offset, connector_id=connector_id
    )
    return StorageFileListResponse(
        items=[StorageFileResponse.from_model(f) for f in files], total=total
    )
