import uuid

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.application.local_agent_service import LocalAgentService, is_online
from app.infrastructure.queue.enrichment_producer import enqueue_enrichment_job
from app.infrastructure.queue.storage_intelligence_producer import enqueue_storage_analysis_job
from app.presentation.api.v1.schemas import (
    AgentBrowseResponse,
    AgentCommandResponse,
    AgentCommandResultRequest,
    AgentCommandsResponse,
    AgentFolderRequest,
    AgentHeartbeatRequest,
    AgentRootsResponse,
    AgentScanBatchRequest,
    AgentScanBatchResponse,
    CreateLocalAgentRequest,
    LocalAgentKeyResponse,
    LocalAgentResponse,
)
from app.presentation.dependencies.auth import get_current_user, require_role
from vault_shared import UnauthorizedError
from vault_shared.db.models import LocalAgent, RoleName, User
from vault_shared.db.session import get_db

local_agents_router = APIRouter(tags=["local-agents"])
_require_owner_or_admin = require_role(RoleName.OWNER, RoleName.ADMIN)


def get_local_agent_service(db: Session = Depends(get_db)) -> LocalAgentService:
    return LocalAgentService(
        db,
        enqueue_enrichment=enqueue_enrichment_job,
        enqueue_storage_analysis=enqueue_storage_analysis_job,
    )


def current_agent(
    authorization: str = Header(default=""),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> LocalAgent:
    """Agents authenticate with their own key (`Authorization: Agent <key>`),
    never with a user session."""
    scheme, _, token = authorization.partition(" ")
    if scheme != "Agent" or not token:
        raise UnauthorizedError("An agent key is required.")
    return service.authenticate(token.strip())


# -- for people ----------------------------------------------------------------


@local_agents_router.post("/local-agents", response_model=LocalAgentKeyResponse, status_code=201)
def create_local_agent(
    request: CreateLocalAgentRequest,
    user: User = Depends(_require_owner_or_admin),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> LocalAgentKeyResponse:
    """Connects a computer: returns a new agent key, shown only this once."""
    connector, key = service.create_agent_key(
        organization_id=user.organization_id, user_id=user.id, name=request.name
    )
    return LocalAgentKeyResponse(connector_id=str(connector.id), agent_key=key)


@local_agents_router.get("/local-agents", response_model=list[LocalAgentResponse])
def list_local_agents(
    user: User = Depends(get_current_user),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> list[LocalAgentResponse]:
    return [
        LocalAgentResponse.from_models(connector, agent, online=is_online(agent))
        for connector, agent in service.list_for_organization(user.organization_id)
    ]


@local_agents_router.post("/local-agents/scan", status_code=202)
def request_local_scan(
    user: User = Depends(_require_owner_or_admin),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> dict:
    """Queues a rescan of every authorized folder on the connected computer."""
    return {"folders": service.request_scan(organization_id=user.organization_id)}


@local_agents_router.get("/local-agents/browse", response_model=AgentBrowseResponse)
def browse_local_folders(
    path: str = Query(min_length=1, max_length=4096),
    user: User = Depends(_require_owner_or_admin),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> AgentBrowseResponse:
    """The folders inside one folder on the connected computer, for picking
    what to give the agent."""
    result = service.browse(organization_id=user.organization_id, path=path)
    return AgentBrowseResponse.model_validate(result)


@local_agents_router.post("/local-agents/roots", response_model=AgentRootsResponse)
def add_local_folder(
    request: AgentFolderRequest,
    user: User = Depends(_require_owner_or_admin),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> AgentRootsResponse:
    result = service.add_root(
        organization_id=user.organization_id, user_id=user.id, path=request.path
    )
    return AgentRootsResponse(roots=result["roots"])


@local_agents_router.post("/local-agents/roots/remove", response_model=AgentRootsResponse)
def remove_local_folder(
    request: AgentFolderRequest,
    user: User = Depends(_require_owner_or_admin),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> AgentRootsResponse:
    """Stops the agent working in a folder; nothing on disk changes."""
    result = service.remove_root(
        organization_id=user.organization_id, user_id=user.id, path=request.path
    )
    return AgentRootsResponse(roots=result["roots"])


# -- for the agent -------------------------------------------------------------


@local_agents_router.post("/agent/heartbeat", status_code=204)
def agent_heartbeat(
    request: AgentHeartbeatRequest,
    agent: LocalAgent = Depends(current_agent),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> None:
    service.heartbeat(
        agent,
        device_name=request.device_name,
        platform=request.platform,
        roots=request.roots,
        offline_roots=request.offline_roots,
        volumes=[volume.model_dump() for volume in request.volumes],
    )


@local_agents_router.post("/agent/scan", response_model=AgentScanBatchResponse)
def agent_scan_batch(
    request: AgentScanBatchRequest,
    agent: LocalAgent = Depends(current_agent),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> AgentScanBatchResponse:
    stored = service.ingest_scan(
        agent,
        root=request.root,
        root_id=request.root_id,
        entries=request.entries,
        first=request.first,
        final=request.final,
    )
    return AgentScanBatchResponse(stored=stored)


@local_agents_router.get("/agent/commands", response_model=AgentCommandsResponse)
def agent_next_commands(
    wait: int = Query(default=20, ge=0, le=25),
    agent: LocalAgent = Depends(current_agent),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> AgentCommandsResponse:
    commands = service.next_commands(agent, wait_seconds=wait)
    return AgentCommandsResponse(
        commands=[
            AgentCommandResponse(id=str(command.id), op=command.op, params=command.params)
            for command in commands
        ]
    )


@local_agents_router.post("/agent/commands/{command_id}/result", status_code=204)
def agent_command_result(
    command_id: uuid.UUID,
    result: AgentCommandResultRequest,
    agent: LocalAgent = Depends(current_agent),
    service: LocalAgentService = Depends(get_local_agent_service),
) -> None:
    service.complete_command(agent, command_id, result.model_dump())
