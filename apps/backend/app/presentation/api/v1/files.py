import uuid
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.application.archive_service import ArchiveService
from app.application.file_service import FileService
from app.application.storage_operation_service import StorageOperationService
from app.presentation.api.v1.schemas import (
    CreatedItemResponse,
    CreateFolderRequest,
    CreateTextFileRequest,
    EmptyTrashRequest,
    FileDetailResponse,
    FileListResponse,
    FileSummaryResponse,
    FolderSummaryResponse,
    TrashSummaryResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.services import (
    get_archive_service,
    get_file_service,
    get_storage_operation_service,
)
from vault_shared.db.models import RoleName, User

files_router = APIRouter(tags=["files"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


@files_router.get("/connectors/{connector_id}/files", response_model=FileListResponse)
def list_files(
    connector_id: uuid.UUID,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ownership: Literal["mine", "shared"] | None = Query(default=None),
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> FileListResponse:
    files, total = service.list_for_connector(
        connector_id,
        organization_id=user.organization_id,
        limit=limit,
        offset=offset,
        ownership=ownership,
    )
    return FileListResponse(items=[FileSummaryResponse.from_model(f) for f in files], total=total)


@files_router.get("/connectors/{connector_id}/trash", response_model=FileListResponse)
def list_trashed_files(
    connector_id: uuid.UUID,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
    archives: ArchiveService = Depends(get_archive_service),
) -> FileListResponse:
    files, total = service.list_trashed_for_connector(
        connector_id, organization_id=user.organization_id, limit=limit, offset=offset
    )
    archived_ids = archives.list_archived_file_ids(user.organization_id)
    return FileListResponse(
        items=[FileSummaryResponse.from_model(f, is_archived=f.id in archived_ids) for f in files],
        total=total,
    )


@files_router.get("/connectors/{connector_id}/folders", response_model=list[FolderSummaryResponse])
def search_folders(
    connector_id: uuid.UUID,
    query: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> list[FolderSummaryResponse]:
    folders = service.search_folders(
        connector_id, organization_id=user.organization_id, query=query, limit=limit
    )
    return [FolderSummaryResponse.from_model(f) for f in folders]


@files_router.get("/files/{file_id}", response_model=FileDetailResponse)
def get_file_detail(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> FileDetailResponse:
    detail = service.get_detail(file_id, organization_id=user.organization_id)
    return FileDetailResponse.from_detail(detail)


@files_router.get("/files/{file_id}/download")
def download_file(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> StreamingResponse:
    _file, stream, content_type, filename = service.get_download_stream(
        file_id, organization_id=user.organization_id
    )
    ascii_filename = filename.encode("ascii", "ignore").decode("ascii").strip() or "download"
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"
        )
    }
    return StreamingResponse(stream, media_type=content_type, headers=headers)


@files_router.post(
    "/connectors/{connector_id}/folders", response_model=CreatedItemResponse, status_code=201
)
def create_folder(
    connector_id: uuid.UUID,
    request: CreateFolderRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> CreatedItemResponse:
    """Creates the folder in the connected storage itself; responds only once
    the provider has confirmed it exists."""
    result = service.create_folder(
        connector_id,
        organization_id=user.organization_id,
        user_id=user.id,
        name=request.name,
        parent_folder_id=uuid.UUID(request.parent_folder_id) if request.parent_folder_id else None,
    )
    return CreatedItemResponse(id=result["id"], name=result["name"], path=result["path"])


@files_router.post(
    "/connectors/{connector_id}/text-files", response_model=CreatedItemResponse, status_code=201
)
def create_text_file(
    connector_id: uuid.UUID,
    request: CreateTextFileRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> CreatedItemResponse:
    result = service.create_text_file(
        connector_id,
        organization_id=user.organization_id,
        user_id=user.id,
        name=request.name,
        content=request.content,
        text_format=request.format,
        parent_folder_id=uuid.UUID(request.parent_folder_id) if request.parent_folder_id else None,
    )
    return CreatedItemResponse(
        id=result["id"],
        name=result["name"],
        path=result["path"],
        web_view_link=result.get("web_view_link"),
    )


@files_router.get("/connectors/{connector_id}/provider-trash", response_model=TrashSummaryResponse)
def preview_provider_trash(
    connector_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> TrashSummaryResponse:
    """What is in the connected storage's own Trash right now, read live from
    the provider."""
    return TrashSummaryResponse.model_validate(
        service.preview_trash(connector_id, organization_id=user.organization_id)
    )


@files_router.post(
    "/connectors/{connector_id}/provider-trash/empty", response_model=TrashSummaryResponse
)
def empty_provider_trash(
    connector_id: uuid.UUID,
    request: EmptyTrashRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> TrashSummaryResponse:
    """Permanently deletes everything in the connected storage's Trash.
    Responds only once the provider confirms the Trash is empty."""
    return TrashSummaryResponse.model_validate(
        service.empty_trash(
            connector_id,
            organization_id=user.organization_id,
            user_id=user.id,
            expected_count=request.expected_count,
            confirmation=request.confirmation,
        )
    )
