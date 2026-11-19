import hashlib
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared import ForbiddenError, NotFoundError, UnauthorizedError, ValidationError
from vault_shared.db.models import (
    AgentCommand,
    ConnectorProvider,
    DriveType,
    EnrichmentTrigger,
    LocalAgent,
    StorageAnalysisTrigger,
    StorageConnector,
)
from vault_shared.db.repositories import (
    AgentCommandRepository,
    AuditLogRepository,
    EnrichmentJobRepository,
    FileRepository,
    FolderRepository,
    LocalAgentRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)

ONLINE_WINDOW = timedelta(seconds=90)
_MAX_WAIT_SECONDS = 25
_CLAIM_LIMIT = 20
_ANSWER_WAIT_SECONDS = 20
_ANSWER_POLL_SECONDS = 0.3


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _holds(mount: str, root: str) -> bool:
    mount_key, root_key = mount.lower().rstrip("/\\"), root.lower()
    return root_key == mount_key or root_key.startswith((mount_key + "/", mount_key + "\\"))


def is_online(agent: LocalAgent | None) -> bool:
    return (
        agent is not None
        and agent.last_seen_at is not None
        and datetime.now(UTC) - agent.last_seen_at < ONLINE_WINDOW
    )


class LocalAgentService:
    """The AI Vault side of the Local Agent: registering a machine, taking in
    what its agent reports, and handing it the typed commands queued by the
    storage adapter. The agent always connects out; nothing here reaches into
    the user's machine."""

    def __init__(
        self,
        db: Session,
        *,
        enqueue_enrichment: Callable[[uuid.UUID], None] | None = None,
        enqueue_storage_analysis: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        self._db = db
        self._enqueue_enrichment = enqueue_enrichment
        self._enqueue_storage_analysis = enqueue_storage_analysis
        self._connectors = StorageConnectorRepository(db)
        self._agents = LocalAgentRepository(db)
        self._commands = AgentCommandRepository(db)
        self._sources = StorageSourceRepository(db)
        self._folders = FolderRepository(db)
        self._files = FileRepository(db)
        self._audit_logs = AuditLogRepository(db)

    # -- for the user ------------------------------------------------------------

    def create_agent_key(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID, name: str
    ) -> tuple[StorageConnector, str]:
        """Connects (or reconnects) this organization's computer. The key is
        returned once and only its hash is kept; a new key replaces the old."""
        connector = self._connectors.upsert_connected(
            organization_id=organization_id,
            provider=ConnectorProvider.LOCAL_AGENT,
            connected_by_user_id=user_id,
            account_email=None,
            workspace_domain=None,
        )
        connector.display_name = name.strip() or "This computer"
        token = secrets.token_urlsafe(32)
        agent = self._agents.get_by_connector_id(connector.id)
        if agent is None:
            self._agents.create(connector_id=connector.id, token_hash=_hash(token))
        else:
            agent.token_hash = _hash(token)
        self._audit_logs.record(
            event_type="local_agent_key_created",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"connector_id": str(connector.id)},
        )
        self._db.commit()
        return connector, token

    def list_for_organization(
        self, organization_id: uuid.UUID
    ) -> list[tuple[StorageConnector, LocalAgent | None]]:
        connector = self._connectors.get_by_organization_and_provider(
            organization_id=organization_id, provider=ConnectorProvider.LOCAL_AGENT
        )
        if connector is None:
            return []
        return [(connector, self._agents.get_by_connector_id(connector.id))]

    def request_scan(self, *, organization_id: uuid.UUID) -> int:
        """Asks the agent to rescan every folder it was authorized for; it
        uploads what it finds when it next checks in."""
        pairs = self.list_for_organization(organization_id)
        connector, agent = pairs[0] if pairs else (None, None)
        if connector is None or agent is None:
            raise NotFoundError("No computer is connected.")
        for root in agent.roots:
            self._commands.enqueue(connector_id=connector.id, op="scan", params={"root": root})
        self._db.commit()
        return len(agent.roots)

    def browse(self, *, organization_id: uuid.UUID, path: str) -> dict:
        """The folders inside `path` on the connected computer, so the user
        can pick one. The agent lists folder names only."""
        return self._ask_agent(organization_id, "browse", {"path": path})

    def add_root(self, *, organization_id: uuid.UUID, user_id: uuid.UUID, path: str) -> dict:
        """Gives the agent a folder or drive. The agent checks it against its
        own safety rules, remembers it, and scans it."""
        result = self._ask_agent(organization_id, "add_root", {"path": path})
        self._audit_root_change("local_agent_folder_added", organization_id, user_id, path)
        return result

    def remove_root(self, *, organization_id: uuid.UUID, user_id: uuid.UUID, path: str) -> dict:
        """Stops the agent working in a folder. Nothing on disk changes."""
        result = self._ask_agent(organization_id, "remove_root", {"path": path})
        agent = self._online_agent(organization_id)
        agent.roots = [root for root in agent.roots if root != path]
        agent.offline_roots = [root for root in agent.offline_roots if root != path]
        for source in self._sources.list_for_connector(agent.connector_id):
            if source.name == path[:255]:
                self._db.delete(source)
        self._audit_root_change("local_agent_folder_removed", organization_id, user_id, path)
        return result

    def _audit_root_change(
        self, event_type: str, organization_id: uuid.UUID, user_id: uuid.UUID, path: str
    ) -> None:
        self._audit_logs.record(
            event_type=event_type,
            organization_id=organization_id,
            user_id=user_id,
            metadata={"path": path},
        )
        self._db.commit()

    def _online_agent(self, organization_id: uuid.UUID) -> LocalAgent:
        pairs = self.list_for_organization(organization_id)
        agent = pairs[0][1] if pairs else None
        if agent is None:
            raise NotFoundError("No computer is connected.")
        if not is_online(agent):
            raise ValidationError("The agent isn't running on that computer — start it first.")
        return agent

    def _ask_agent(self, organization_id: uuid.UUID, op: str, params: dict) -> dict:
        """Queues one typed command and waits for the agent's answer."""
        agent = self._online_agent(organization_id)
        command = self._commands.enqueue(connector_id=agent.connector_id, op=op, params=params)
        self._db.commit()
        deadline = time.monotonic() + _ANSWER_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_ANSWER_POLL_SECONDS)
            self._db.refresh(command)
            if command.result is not None:
                result = dict(command.result)
                if result.get("ok"):
                    return result
                message = str(result.get("error") or "The agent could not do that.")
                if result.get("kind") == "not_found":
                    raise NotFoundError(message)
                raise ValidationError(message)
        raise ValidationError("The agent didn't answer in time — is it still running?")

    # -- for the agent -------------------------------------------------------------

    def authenticate(self, token: str) -> LocalAgent:
        agent = self._agents.get_by_token_hash(_hash(token)) if token else None
        if agent is None:
            raise UnauthorizedError("Unknown agent key.")
        return agent

    def heartbeat(
        self,
        agent: LocalAgent,
        *,
        device_name: str,
        platform: str,
        roots: list[str],
        volumes: list[dict],
        offline_roots: list[str] | None = None,
    ) -> None:
        offline_roots = offline_roots or []
        self._agents.record_heartbeat(
            agent,
            device_name=device_name,
            platform=platform,
            roots=roots,
            offline_roots=offline_roots,
            volumes=volumes,
        )
        # A folder the agent no longer reports was taken out of its authorized
        # list; nothing in it can be acted on, so it leaves the index. A folder
        # on an unplugged drive is still reported (as offline) and stays.
        kept = {root[:255] for root in [*roots, *offline_roots]}
        for source in self._sources.list_for_connector(agent.connector_id):
            if source.name not in kept:
                self._db.delete(source)
        # Storage use counts only the drives holding the agent's folders.
        holding: dict[str, dict] = {}
        for root in roots:
            holders = [v for v in volumes if _holds(str(v.get("mount", "")), root)]
            if holders:
                volume = max(holders, key=lambda v: len(str(v.get("mount", ""))))
                holding[volume["mount"]] = volume
        in_use = list(holding.values())
        connector = self._connector(agent)
        connector.account_email = None
        connector.workspace_domain = device_name[:255]
        connector.last_verified_at = datetime.now(UTC)
        connector.storage_used_bytes = sum(int(v.get("used_bytes", 0)) for v in in_use) or None
        connector.storage_total_bytes = sum(int(v.get("total_bytes", 0)) for v in in_use) or None
        connector.quota_checked_at = datetime.now(UTC)
        self._db.commit()

    def ingest_scan(
        self,
        agent: LocalAgent,
        *,
        root: str,
        root_id: str,
        entries: list[dict],
        first: bool,
        final: bool,
    ) -> int:
        """Records one batch of what the agent found. Entries arrive parents
        first, so every folder exists before anything inside it. When the
        final batch arrives, files that weren't seen are marked as gone."""
        if root not in agent.roots:
            raise ForbiddenError("That folder isn't one this agent was authorized for.")
        now = datetime.now(UTC)
        if first or agent.scan_started_at is None:
            agent.scan_started_at = now
        source = self._sources.upsert(
            connector_id=agent.connector_id,
            provider_drive_id=root_id,
            # The full path: it is how the agent names an authorized folder.
            name=root[:255],
            drive_type=DriveType.LOCAL_FOLDER,
        )
        stored = 0
        for entry in entries:
            if entry.get("is_link"):
                continue
            parent_id = entry.get("parent_id")
            parent = (
                None
                if parent_id in (None, root_id)
                else self._folders.get_by_source_and_provider_id(
                    storage_source_id=source.id, provider_file_id=parent_id
                )
            )
            path = "/" + entry["relative_path"]
            if entry.get("is_dir"):
                self._folders.upsert(
                    storage_source_id=source.id,
                    provider_file_id=entry["id"],
                    provider_parent_id=parent_id,
                    parent_folder_id=parent.id if parent else None,
                    name=entry["name"],
                    path=path,
                    owner_email=entry.get("owner"),
                    is_shared=False,
                    provider_created_at=_time(entry.get("created_at")),
                    provider_modified_at=_time(entry.get("modified_at")),
                    scanned_at=now,
                )
            else:
                self._files.upsert(
                    storage_source_id=source.id,
                    provider_file_id=entry["id"],
                    provider_parent_id=parent_id,
                    parent_folder_id=parent.id if parent else None,
                    name=entry["name"],
                    path=path,
                    mime_type=entry.get("mime_type"),
                    size_bytes=entry.get("size_bytes"),
                    owner_email=entry.get("owner"),
                    is_shared=False,
                    permissions_summary=None,
                    version_id=None,
                    checksum=entry.get("sha256"),
                    web_view_link=None,
                    provider_created_at=_time(entry.get("created_at")),
                    provider_modified_at=_time(entry.get("modified_at")),
                    provider_viewed_at=_time(entry.get("accessed_at")),
                    scanned_at=now,
                )
            stored += 1
        if final:
            started = agent.scan_started_at or now
            for file in self._files.list_for_source(source.id):
                if file.scanned_at < started and file.permanently_deleted_at is None:
                    self._files.record_observed_removal(
                        storage_source_id=source.id, provider_file_id=file.provider_file_id
                    )
            self._connector(agent).last_synced_at = now
            agent.scan_started_at = None
        self._db.commit()
        if final:
            self._after_scan(self._connector(agent))
        return stored

    def _after_scan(self, connector: StorageConnector) -> None:
        """What a finished Drive scan sets off, for a finished local scan
        too: reading the files (so search and Ask Vault know their content)
        and refreshing Storage Intelligence."""
        enrichment_jobs = EnrichmentJobRepository(self._db)
        if self._enqueue_enrichment and not enrichment_jobs.has_active_job(connector.id):
            enrichment = enrichment_jobs.create(
                connector_id=connector.id,
                triggered_by=EnrichmentTrigger.SCAN_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            self._enqueue_enrichment(enrichment.id)
        analysis_jobs = StorageAnalysisJobRepository(self._db)
        if self._enqueue_storage_analysis and not analysis_jobs.has_active_job(
            connector.organization_id
        ):
            analysis = analysis_jobs.create(
                organization_id=connector.organization_id,
                triggered_by=StorageAnalysisTrigger.SCAN_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            self._enqueue_storage_analysis(analysis.id)

    def next_commands(self, agent: LocalAgent, *, wait_seconds: int) -> list[AgentCommand]:
        """Long poll: returns as soon as something is queued, or empty after
        `wait_seconds`."""
        deadline = time.monotonic() + min(max(wait_seconds, 0), _MAX_WAIT_SECONDS)
        while True:
            commands = self._commands.claim_pending(agent.connector_id, limit=_CLAIM_LIMIT)
            self._db.commit()
            if commands or time.monotonic() >= deadline:
                return commands
            time.sleep(0.5)

    def complete_command(self, agent: LocalAgent, command_id: uuid.UUID, result: dict) -> None:
        command = self._commands.get(command_id)
        if command is None or command.connector_id != agent.connector_id:
            raise NotFoundError("Command not found.")
        if not isinstance(result.get("ok"), bool):
            raise ValidationError("A result must say whether it succeeded.")
        self._commands.complete(command, result=result)
        self._db.commit()

    def _connector(self, agent: LocalAgent) -> StorageConnector:
        connector = self._connectors.get_by_id(agent.connector_id)
        assert connector is not None
        return connector
