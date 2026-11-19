"""The whole local chain, for real on disk: a Local Agent on a temporary
folder, AI Vault's agent service taking in its scan and handing it commands,
and the Execution Engine changing local files through `LocalAgentAdapter` —
each change checked on disk and in AI Vault's index."""

import os
import socket
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
from aivault_agent.agent_service import AgentService, _iso
from sqlalchemy.orm import Session

from app.application.local_agent_service import LocalAgentService
from vault_shared import ValidationError, get_settings
from vault_shared.db.models import ExecutionActionType, File, RoleName, StorageSource
from vault_shared.db.repositories import (
    FileRepository,
    FolderRepository,
    LocalAgentRepository,
    OrganizationRepository,
    RoleRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution.plan_service import ExecutionPlanService
from vault_shared.storage.default_registry import build_storage_registry
from worker.execution.execution_service import ExecutionService


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except (OSError, TypeError):
        return False


requires_infra = pytest.mark.skipif(
    not (_reachable(get_settings().database_url) and _reachable(get_settings().redis_url)),
    reason="Postgres/Redis not reachable.",
)


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _Machine:
    """A user's computer: the agent on a temporary folder, connected to AI
    Vault, with its polling loop running in a background thread."""

    def __init__(self, db: Session, root: Path) -> None:
        unique = uuid.uuid4().hex[:12]
        organization = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
        self.user = UserRepository(db).create(
            organization_id=organization.id,
            role_id=RoleRepository(db).get_by_name(RoleName.OWNER).id,
            google_sub=f"sub-{unique}",
            email=f"founder-{unique}@example.com",
            name="Ada",
            avatar_url=None,
        )
        db.commit()
        self.connector, self.key = LocalAgentService(db).create_agent_key(
            organization_id=organization.id, user_id=self.user.id, name="Laptop"
        )
        self.agent = AgentService([root])
        self.root = root
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def report_in_and_scan(self, db: Session) -> None:
        service = LocalAgentService(db)
        agent_row = service.authenticate(self.key)
        service.heartbeat(
            agent_row, device_name="laptop", platform="test", roots=[str(self.root)], volumes=[]
        )
        summary = self.agent.handle({"op": "scan", "params": {"root": str(self.root)}})
        entries = [
            {k: _iso(v) for k, v in asdict(entry).items()} for entry in self.agent.last_scan.entries
        ]
        service.ingest_scan(
            agent_row,
            root=summary["root"],
            root_id=summary["root_id"],
            entries=entries,
            first=True,
            final=True,
        )

    def _serve(self) -> None:
        session = get_session_factory()()
        try:
            service = LocalAgentService(session)
            agent_row = service.authenticate(self.key)
            while not self._stop.is_set():
                for command in service.next_commands(agent_row, wait_seconds=1):
                    result = self.agent.handle({"op": command.op, "params": command.params})
                    service.complete_command(agent_row, command.id, result)
        finally:
            session.close()

    def __enter__(self) -> "_Machine":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _run_action(db: Session, machine: _Machine, file_ids: list[uuid.UUID], **kwargs) -> None:  # noqa: ANN003
    queued: list[uuid.UUID] = []
    ExecutionPlanService(db, enqueue_execution_job=queued.append).create_ad_hoc_plan(
        file_ids,
        organization_id=machine.user.organization_id,
        user_id=machine.user.id,
        require_approval=False,
        **kwargs,
    )
    [job_id] = queued
    ExecutionService(
        db,
        storage=build_storage_registry(db, oauth_client=MagicMock()),
        object_storage_client=MagicMock(),
    ).run(job_id)
    db.expire_all()


@pytest.fixture
def machine(db: Session, tmp_path: Path):
    root = tmp_path / "Laptop Projects"
    (root / "Inbox").mkdir(parents=True)
    (root / "Blarrow").mkdir()
    (root / "Inbox" / "proposal-final2.pdf").write_bytes(b"%PDF proposal")
    machine = _Machine(db, root)
    machine.report_in_and_scan(db)
    with machine:
        yield machine


def _indexed(db: Session, machine: _Machine, name: str) -> File:
    """A file in this test's connection only — other tests' data shares names."""
    return (
        db.query(File)
        .join(StorageSource, StorageSource.id == File.storage_source_id)
        .filter(StorageSource.connector_id == machine.connector.id, File.name == name)
        .one()
    )


@requires_infra
def test_a_scan_puts_the_machines_files_in_ai_vault(db: Session, machine: _Machine) -> None:
    agent_row = LocalAgentRepository(db).get_by_connector_id(machine.connector.id)
    proposal = _indexed(db, machine, "proposal-final2.pdf")

    assert agent_row.roots == [str(machine.root)]
    assert proposal.path == "/Inbox/proposal-final2.pdf"
    assert proposal.checksum is not None


@requires_infra
def test_renaming_through_ai_vault_renames_the_real_file(db: Session, machine: _Machine) -> None:
    proposal = _indexed(db, machine, "proposal-final2.pdf")

    _run_action(
        db,
        machine,
        [proposal.id],
        action_type=ExecutionActionType.RENAME,
        new_name="Blarrow_Proposal_2026_Final.pdf",
    )

    assert (machine.root / "Inbox" / "Blarrow_Proposal_2026_Final.pdf").exists()
    assert not (machine.root / "Inbox" / "proposal-final2.pdf").exists()
    assert FileRepository(db).get_by_id(proposal.id).name == "Blarrow_Proposal_2026_Final.pdf"


@requires_infra
def test_moving_through_ai_vault_moves_the_real_file(db: Session, machine: _Machine) -> None:
    proposal = _indexed(db, machine, "proposal-final2.pdf")
    blarrow = next(
        folder
        for folder in FolderRepository(db).list_for_source(proposal.storage_source_id)
        if folder.name == "Blarrow"
    )

    _run_action(
        db,
        machine,
        [proposal.id],
        action_type=ExecutionActionType.MOVE_FILE,
        new_parent_id=blarrow.provider_file_id,
    )

    assert (machine.root / "Blarrow" / "proposal-final2.pdf").exists()
    assert FileRepository(db).get_by_id(proposal.id).path == "/Blarrow/proposal-final2.pdf"


@requires_infra
def test_trash_and_restore_through_ai_vault(db: Session, machine: _Machine) -> None:
    proposal = _indexed(db, machine, "proposal-final2.pdf")

    _run_action(db, machine, [proposal.id], action_type=ExecutionActionType.ARCHIVE)
    trashed_on_disk = not (machine.root / "Inbox" / "proposal-final2.pdf").exists()
    trashed_in_index = FileRepository(db).get_by_id(proposal.id).trashed

    _run_action(db, machine, [proposal.id], action_type=ExecutionActionType.RESTORE)

    assert trashed_on_disk and trashed_in_index
    assert (machine.root / "Inbox" / "proposal-final2.pdf").read_bytes() == b"%PDF proposal"
    assert FileRepository(db).get_by_id(proposal.id).trashed is False


def _org(machine: _Machine) -> uuid.UUID:
    return machine.user.organization_id


@requires_infra
def test_browsing_shows_the_folders_on_the_computer(db: Session, machine: _Machine) -> None:
    result = LocalAgentService(db).browse(organization_id=_org(machine), path=str(machine.root))

    assert [folder["name"] for folder in result["folders"]] == ["Blarrow", "Inbox"]
    assert all(folder["can_add"] is False for folder in result["folders"])


@requires_infra
def test_picking_a_folder_gives_it_to_the_agent(
    db: Session, machine: _Machine, tmp_path: Path
) -> None:
    (tmp_path / "Photos").mkdir()

    result = LocalAgentService(db).add_root(
        organization_id=_org(machine), user_id=machine.user.id, path=str(tmp_path / "Photos")
    )

    assert str((tmp_path / "Photos").resolve()) in result["roots"]
    assert machine.agent.authorized.covers(tmp_path / "Photos")


@requires_infra
def test_a_system_folder_is_refused_with_the_agents_reason(db: Session, machine: _Machine) -> None:
    system = os.environ.get("SYSTEMROOT", r"C:\Windows") if sys.platform == "win32" else "/usr"

    with pytest.raises(ValidationError, match="system"):
        LocalAgentService(db).add_root(
            organization_id=_org(machine), user_id=machine.user.id, path=system
        )


@requires_infra
def test_removing_a_folder_takes_it_out_of_ai_vault_but_not_off_the_disk(
    db: Session, machine: _Machine
) -> None:
    LocalAgentService(db).remove_root(
        organization_id=_org(machine), user_id=machine.user.id, path=str(machine.root)
    )
    db.expire_all()

    sources = db.query(StorageSource).filter_by(connector_id=machine.connector.id).all()
    assert sources == []
    assert machine.agent.authorized.roots == []
    assert (machine.root / "Inbox" / "proposal-final2.pdf").exists()


@requires_infra
def test_asking_a_computer_whose_agent_is_not_running_fails_at_once(
    db: Session, machine: _Machine
) -> None:
    agent_row = LocalAgentRepository(db).get_by_connector_id(machine.connector.id)
    agent_row.last_seen_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()

    with pytest.raises(ValidationError, match="isn't running"):
        LocalAgentService(db).browse(organization_id=_org(machine), path=str(machine.root))


@requires_infra
def test_a_finished_scan_starts_reading_the_files_and_refreshes_storage_intelligence(
    db: Session, machine: _Machine
) -> None:
    enrichment: list[uuid.UUID] = []
    analysis: list[uuid.UUID] = []
    service = LocalAgentService(
        db, enqueue_enrichment=enrichment.append, enqueue_storage_analysis=analysis.append
    )
    agent_row = service.authenticate(machine.key)
    summary, result = machine.agent.scan_root(str(machine.root))
    entries = [{k: _iso(v) for k, v in asdict(e).items()} for e in result.entries]

    service.ingest_scan(
        agent_row,
        root=summary["root"],
        root_id=summary["root_id"],
        entries=entries,
        first=True,
        final=True,
    )

    assert len(enrichment) == 1
    assert len(analysis) == 1
