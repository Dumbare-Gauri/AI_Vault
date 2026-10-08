import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.application.archive_service import ArchiveService
from app.presentation.api.v1.schemas import ArchiveJobDetailResponse, ArchiveJobResponse
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.services import get_archive_service
from vault_shared.db.models import RoleName, User

archives_router = APIRouter(tags=["archives"])

_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)


@archives_router.get("/archives", response_model=list[ArchiveJobResponse])
def list_archives(
    user: User = Depends(get_current_user),
    service: ArchiveService = Depends(get_archive_service),
) -> list[ArchiveJobResponse]:
    archive_jobs, _total = service.list_for_organization(user.organization_id)
    return [ArchiveJobResponse.from_model(job) for job in archive_jobs]


@archives_router.get("/archives/{archive_id}", response_model=ArchiveJobDetailResponse)
def get_archive(
    archive_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ArchiveService = Depends(get_archive_service),
) -> ArchiveJobDetailResponse:
    archive_job = service.get_owned(archive_id, organization_id=user.organization_id)
    return ArchiveJobDetailResponse.from_model_detail(archive_job)


@archives_router.get("/archives/{archive_id}/download")
def download_archive(
    archive_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ArchiveService = Depends(get_archive_service),
) -> StreamingResponse:
    archive_job, stream = service.get_download_stream(
        archive_id, organization_id=user.organization_id
    )
    filename = f"{archive_job.name}.zip".replace('"', "")
    # A plain `filename=` value must be latin-1/ASCII-safe (Starlette encodes
    # every header value that way) — an archive name with e.g. an em-dash or
    # any other non-ASCII character would otherwise crash header construction
    # entirely. `filename*` (RFC 5987/6266) carries the real UTF-8 name for
    # browsers that support it; the ASCII fallback covers the rest.
    ascii_filename = filename.encode("ascii", "ignore").decode("ascii").strip() or "archive.zip"
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"
        )
    }
    return StreamingResponse(stream, media_type="application/zip", headers=headers)


@archives_router.delete("/archives/{archive_id}", status_code=204)
def delete_archive(
    archive_id: uuid.UUID,
    user: User = Depends(_require_owner_or_admin),
    service: ArchiveService = Depends(get_archive_service),
) -> None:
    service.delete(archive_id, organization_id=user.organization_id, user_id=user.id)
