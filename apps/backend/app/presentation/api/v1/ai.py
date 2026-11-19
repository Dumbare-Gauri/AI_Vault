from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application.ai_status_service import get_ai_status
from app.presentation.api.v1.schemas import AIStatusResponse
from app.presentation.dependencies.auth import get_current_user
from vault_shared.ai_gateway import AIGateway, get_ai_gateway
from vault_shared.db.models import User
from vault_shared.db.session import get_db

ai_router = APIRouter(tags=["ai"])


@ai_router.get("/ai/status", response_model=AIStatusResponse)
def ai_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gateway: AIGateway = Depends(get_ai_gateway),
) -> AIStatusResponse:
    status = get_ai_status(db, user.organization_id, gateway)
    return AIStatusResponse(
        mode=status.mode, source=status.source, provider=status.provider, model=status.model
    )
