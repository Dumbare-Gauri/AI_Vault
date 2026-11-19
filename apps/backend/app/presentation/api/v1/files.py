import uuid
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.application.archive_service import ArchiveService
from app.application.file_service import FileService
from app.application.storage_operation_service import StorageOperationService
from app.presentation.api.v1.schemas import (
    ArchiveContentsResponse,
    CopyFileRequest,
    CreatedItemResponse,
    CreateFolderRequest,
    CreateTextFileRequest,
    EmptyTrashRequest,
    EmptyTrashStartedResponse,
    EmptyTrashStatusResponse,
    ExtractArchiveRequest,
    ExtractedArchiveResponse,
    FileDetailResponse,
    FileListResponse,
    FileSummaryResponse,
    FolderListingResponse,
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


@files_router.get("/connectors/{connector_id}/browse", response_model=FolderListingResponse)
def browse_folder(
    connector_id: uuid.UUID,
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    source_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> FolderListingResponse:
    """One folder of a connected storage as the storage arranges it:
    breadcrumbs, the folders directly inside it, and a page of its files."""
    listing = service.browse(
        connector_id,
        organization_id=user.organization_id,
        source_id=source_id,
        folder_id=folder_id,
        limit=limit,
        offset=offset,
    )
    return FolderListingResponse.from_listing(listing)


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
        source_id=uuid.UUID(request.source_id) if request.source_id else None,
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
        source_id=uuid.UUID(request.source_id) if request.source_id else None,
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
    "/connectors/{connector_id}/provider-trash/empty",
    response_model=EmptyTrashStartedResponse,
    status_code=202,
)
def empty_provider_trash(
    connector_id: uuid.UUID,
    request: EmptyTrashRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> EmptyTrashStartedResponse:
    """Starts permanently deleting everything in the connected storage's
    Trash. Poll the returned operation for the provider-confirmed result."""
    return EmptyTrashStartedResponse(
        operation_id=service.start_empty_trash(
            connector_id,
            organization_id=user.organization_id,
            user_id=user.id,
            expected_count=request.expected_count,
            confirmed=request.confirmed,
        )
    )


@files_router.get(
    "/connectors/{connector_id}/provider-trash/empty/{operation_id}",
    response_model=EmptyTrashStatusResponse,
)
def empty_provider_trash_status(
    connector_id: uuid.UUID,
    operation_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> EmptyTrashStatusResponse:
    return EmptyTrashStatusResponse.model_validate(
        service.empty_trash_status(
            connector_id, str(operation_id), organization_id=user.organization_id
        )
    )


@files_router.get("/files/{file_id}/archive-contents", response_model=ArchiveContentsResponse)
def archive_contents(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> ArchiveContentsResponse:
    """Lists what a zip holds — read only, nothing is unpacked."""
    return ArchiveContentsResponse.model_validate(
        service.inspect_archive(file_id, organization_id=user.organization_id)
    )


@files_router.post(
    "/files/{file_id}/extract", response_model=ExtractedArchiveResponse, status_code=201
)
def extract_archive(
    file_id: uuid.UUID,
    request: ExtractArchiveRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> ExtractedArchiveResponse:
    """Unpacks a zip into a new folder in the same storage — never over
    existing files, never outside that folder — and answers with what the
    storage confirmed."""
    result = service.extract_archive(
        file_id,
        organization_id=user.organization_id,
        user_id=user.id,
        destination_folder_id=(
            uuid.UUID(request.destination_folder_id) if request.destination_folder_id else None
        ),
    )
    return ExtractedArchiveResponse(
        id=result["id"],
        name=result["name"],
        path=result["path"],
        file_count=result["file_count"],
        skipped=result.get("skipped") or [],
    )


@files_router.post("/files/{file_id}/copy", response_model=CreatedItemResponse, status_code=201)
def make_copy(
    file_id: uuid.UUID,
    request: CopyFileRequest,
    user: User = Depends(_require_owner_or_admin),
    service: StorageOperationService = Depends(get_storage_operation_service),
) -> CreatedItemResponse:
    """Copies the file in the connected storage itself, next to the original;
    responds only once the provider's copy has been checked against it."""
    result = service.make_copy(
        file_id, organization_id=user.organization_id, user_id=user.id, new_name=request.new_name
    )
    return CreatedItemResponse(
        id=result["id"],
        name=result["name"],
        path=result["path"],
        web_view_link=result.get("web_view_link"),
    )
