from fastapi import APIRouter, Depends, Request, Response

from app.application.account_deletion_service import AccountDeletionService
from app.presentation.api.v1.request_utils import client_ip
from app.presentation.api.v1.schemas import (
    AccountDeletionResponse,
    DeleteAccountRequest,
    UserProfileResponse,
)
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import get_account_deletion_service
from vault_shared import get_settings
from vault_shared.db.models import User

users_router = APIRouter(prefix="/users", tags=["users"])

_delete_rate_limit = rate_limiter("account-delete", limit=5, window_seconds=300)


@users_router.get("/me", response_model=UserProfileResponse)
def get_my_profile(user: User = Depends(get_current_user)) -> UserProfileResponse:
    return UserProfileResponse.from_model(user)


@users_router.post(
    "/me/delete",
    response_model=AccountDeletionResponse,
    dependencies=[Depends(_delete_rate_limit)],
)
def delete_my_account(
    body: DeleteAccountRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    service: AccountDeletionService = Depends(get_account_deletion_service),
) -> AccountDeletionResponse:
    """Deletes the signed-in person's account and all of AI Vault's data
    about them. Files in their own Google Drive or on their computer are
    not touched."""
    summary = service.delete_account(
        user, confirm_email=body.confirm_email, ip_address=client_ip(request)
    )
    response.delete_cookie(get_settings().refresh_cookie_name, path="/v1/auth")
    return AccountDeletionResponse(
        connections_removed=summary.connections_removed,
        tokens_revoked=summary.tokens_revoked,
        files_unindexed=summary.files_unindexed,
        cached_archives_deleted=summary.cached_archives_deleted,
    )
