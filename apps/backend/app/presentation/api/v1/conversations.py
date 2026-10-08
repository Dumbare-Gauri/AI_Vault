import uuid

from fastapi import APIRouter, Depends, Query

from app.application.conversation_service import ConversationService
from app.application.vault_action_service import VaultActionService
from app.presentation.api.v1.schemas import (
    AskRequest,
    AskResponse,
    ConversationDetailResponse,
    ConversationMessageResponse,
    ConversationResponse,
    ResultPageResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from app.presentation.dependencies.rate_limit import rate_limiter
from app.presentation.dependencies.services import (
    get_conversation_service,
    get_vault_action_service,
)
from vault_shared.db.models import RoleName, User

conversations_router = APIRouter(tags=["conversations"])

# Phase 10 (ADR-022) — every `ask()` call assembles RAG context and invokes
# the AI Gateway's completion provider; rate-limited the same as search.
_ask_rate_limit = rate_limiter("conversation-ask", limit=30, window_seconds=60)
_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)


@conversations_router.get("/conversations", response_model=list[ConversationResponse])
def list_conversations(
    user: User = Depends(get_current_user),
    service: ConversationService = Depends(get_conversation_service),
) -> list[ConversationResponse]:
    conversations = service.list_for_user(organization_id=user.organization_id, user_id=user.id)
    return [ConversationResponse.from_model(c) for c in conversations]


@conversations_router.get(
    "/conversations/{conversation_id}", response_model=ConversationDetailResponse
)
def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationDetailResponse:
    detail = service.get_detail(
        conversation_id, organization_id=user.organization_id, user_id=user.id
    )
    return ConversationDetailResponse.from_detail(detail)


@conversations_router.post(
    "/conversations",
    response_model=AskResponse,
    status_code=201,
    dependencies=[Depends(_ask_rate_limit)],
)
def start_conversation(
    request: AskRequest,
    user: User = Depends(get_current_user),
    service: ConversationService = Depends(get_conversation_service),
) -> AskResponse:
    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=None,
        question=request.question,
    )
    return AskResponse.from_turn(turn)


@conversations_router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AskResponse,
    status_code=201,
    dependencies=[Depends(_ask_rate_limit)],
)
def send_message(
    conversation_id: uuid.UUID,
    request: AskRequest,
    user: User = Depends(get_current_user),
    service: ConversationService = Depends(get_conversation_service),
) -> AskResponse:
    turn = service.ask(
        organization_id=user.organization_id,
        user_id=user.id,
        conversation_id=conversation_id,
        question=request.question,
    )
    return AskResponse.from_turn(turn)


@conversations_router.get(
    "/conversations/{conversation_id}/messages/{message_id}/blocks/{block_index}",
    response_model=ResultPageResponse,
)
def get_result_page(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    block_index: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
    user: User = Depends(get_current_user),
    service: ConversationService = Depends(get_conversation_service),
) -> ResultPageResponse:
    """The next page of a file list or duplicate list from an answer."""
    page = service.result_page(
        conversation_id,
        message_id,
        block_index,
        organization_id=user.organization_id,
        user_id=user.id,
        offset=offset,
        limit=limit,
    )
    return ResultPageResponse(**page)


@conversations_router.post(
    "/conversations/{conversation_id}/actions/{action_id}/confirm",
    response_model=ConversationMessageResponse,
    status_code=201,
)
def confirm_action(
    conversation_id: uuid.UUID,
    action_id: str,
    user: User = Depends(_require_owner_or_admin),
    service: VaultActionService = Depends(get_vault_action_service),
) -> ConversationMessageResponse:
    """Carries out a change Ask Vault proposed — only ever after this
    explicit confirmation, and only through the Execution Engine."""
    message = service.confirm(
        conversation_id, action_id, organization_id=user.organization_id, user_id=user.id
    )
    return ConversationMessageResponse.from_model(message)


@conversations_router.post(
    "/conversations/{conversation_id}/actions/{action_id}/cancel",
    response_model=ConversationMessageResponse,
    status_code=201,
)
def cancel_action(
    conversation_id: uuid.UUID,
    action_id: str,
    user: User = Depends(get_current_user),
    service: VaultActionService = Depends(get_vault_action_service),
) -> ConversationMessageResponse:
    message = service.cancel(
        conversation_id, action_id, organization_id=user.organization_id, user_id=user.id
    )
    return ConversationMessageResponse.from_model(message)
