import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import AgentCommand, AgentCommandStatus, LocalAgent


class LocalAgentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, connector_id: uuid.UUID, token_hash: str) -> LocalAgent:
        agent = LocalAgent(connector_id=connector_id, token_hash=token_hash, roots=[], volumes=[])
        self._session.add(agent)
        self._session.flush()
        return agent

    def get_by_token_hash(self, token_hash: str) -> LocalAgent | None:
        return self._session.query(LocalAgent).filter_by(token_hash=token_hash).first()

    def get_by_connector_id(self, connector_id: uuid.UUID) -> LocalAgent | None:
        return self._session.query(LocalAgent).filter_by(connector_id=connector_id).first()

    def record_heartbeat(
        self,
        agent: LocalAgent,
        *,
        device_name: str,
        platform: str,
        roots: list[str],
        offline_roots: list[str],
        volumes: list[dict],
    ) -> None:
        agent.device_name = device_name
        agent.platform = platform
        agent.roots = roots
        agent.offline_roots = offline_roots
        agent.volumes = volumes
        agent.last_seen_at = datetime.now(UTC)
        self._session.flush()


class AgentCommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, *, connector_id: uuid.UUID, op: str, params: dict) -> AgentCommand:
        command = AgentCommand(connector_id=connector_id, op=op, params=params)
        self._session.add(command)
        self._session.flush()
        return command

    def get(self, command_id: uuid.UUID) -> AgentCommand | None:
        return self._session.get(AgentCommand, command_id)

    def claim_pending(self, connector_id: uuid.UUID, *, limit: int) -> list[AgentCommand]:
        """Hands pending commands to the agent exactly once: rows are locked
        while claimed, so two polls can never receive the same command."""
        commands = (
            self._session.query(AgentCommand)
            .filter_by(connector_id=connector_id, status=AgentCommandStatus.PENDING)
            .order_by(AgentCommand.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        now = datetime.now(UTC)
        for command in commands:
            command.status = AgentCommandStatus.SENT
            command.sent_at = now
        self._session.flush()
        return commands

    def complete(self, command: AgentCommand, *, result: dict) -> None:
        command.result = result
        command.status = AgentCommandStatus.DONE if result.get("ok") else AgentCommandStatus.FAILED
        command.completed_at = datetime.now(UTC)
        self._session.flush()
