"""Google Drive and this computer are separate workspaces.

One organization has both connected, with look-alike files in each (the same
names, the same invoice text, identical content). Every way of reaching files
— search by name, by content and by similarity; Ask Vault answers, citations
and proposals; duplicate groups; plans; confirmations; dashboards — must stay
inside the workspace it was asked in. Disposable data only; no real storage.
"""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_vault_assistant_integration import _gateway, requires_infra

from app.application.conversation_service import ConversationService, PageContext
from app.application.dashboard_service import DashboardService
from app.application.search_service import SearchService
from app.application.vault_action_service import VaultActionService
from app.presentation.dependencies.workspace import resolve_workspace
from vault_shared import ConflictError, NotFoundError, ValidationError
from vault_shared.db.models import (
    ConnectorProvider,
    DriveType,
    ExecutionActionType,
    RoleName,
    StorageAnalysisTrigger,
)
from vault_shared.db.repositories import (
    DuplicateGroupRepository,
    EmbeddingRepository,
    FileExtractionRepository,
    FileRepository,
    OrganizationRepository,
    RoleRepository,
    StorageAnalysisJobRepository,
    StorageAnalysisSnapshotRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
    UserRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import ExecutionPlanService
from vault_shared.search import FileQuery
from worker.storage_intelligence.storage_intelligence_service import StorageIntelligenceService

_INVOICE = "Invoice 2026-031 from Blarrow Ltd. Payment of 4,200 EUR is due on 30 November."


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _TwoWorkspaces:
    """Google Drive and this computer, connected to one organization, each
    holding look-alikes of the other's files."""

    def __init__(self, db: Session) -> None:
        unique = uuid.uuid4().hex[:12]
        self.db = db
        self.org = OrganizationRepository(db).create(name="Acme", slug=f"acme-{unique}")
        self.user = UserRepository(db).create(
            organization_id=self.org.id,
            role_id=RoleRepository(db).get_by_name(RoleName.OWNER).id,
            google_sub=f"sub-{unique}",
            email=f"ada-{unique}@example.com",
            name="Ada",
            avatar_url=None,
        )
        connectors = StorageConnectorRepository(db)
        self.drive = connectors.upsert_connected(
            organization_id=self.org.id,
            provider=ConnectorProvider.GOOGLE_WORKSPACE,
            connected_by_user_id=self.user.id,
            account_email=None,
            workspace_domain=None,
        )
        self.local = connectors.upsert_connected(
            organization_id=self.org.id,
            provider=ConnectorProvider.LOCAL_AGENT,
            connected_by_user_id=self.user.id,
            account_email=None,
            workspace_domain=None,
        )
        for connector in (self.drive, self.local):
            connector.last_synced_at = datetime.now(UTC)
        sources = StorageSourceRepository(db)
        self.drive_source = sources.upsert(
            connector_id=self.drive.id,
            provider_drive_id=f"root-{unique}",
            name="My Drive",
            drive_type=DriveType.MY_DRIVE,
        )
        self.local_source = sources.upsert(
            connector_id=self.local.id,
            provider_drive_id=f"disk-{unique}",
            name="C:\\Work",
            drive_type=DriveType.LOCAL_FOLDER,
        )
        db.commit()

    def file(self, where: str, path: str, *, text: str = "", checksum: str | None = None):  # noqa: ANN201
        source = self.drive_source if where == "drive" else self.local_source
        name = path.rsplit("/", 1)[-1]
        now = datetime.now(UTC)
        file = FileRepository(self.db).upsert(
            storage_source_id=source.id,
            # The same provider id in both storages: identity is never the id alone.
            provider_file_id=f"id-{path}",
            provider_parent_id=None,
            parent_folder_id=None,
            name=name,
            path=path,
            mime_type="application/pdf",
            size_bytes=1000,
            owner_email=None,
            is_shared=False,
            permissions_summary=None,
            version_id=None,
            checksum=checksum,
            web_view_link=None,
            provider_created_at=now,
            provider_modified_at=now,
            provider_viewed_at=None,
            scanned_at=now,
        )
        if text:
            FileExtractionRepository(self.db).upsert(
                file_id=file.id,
                status="success",
                extractor_name="pdf_text",
                extracted_text=text,
                char_count=len(text),
                error=None,
                extracted_at=now,
            )
            [vector] = _gateway().embed([text])
            EmbeddingRepository(self.db).upsert(
                file_id=file.id,
                model_name=vector.model_name,
                model_version=vector.model_version,
                dimensions=vector.dimensions,
                vector=vector.vector,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                embedded_at=now,
            )
        self.db.commit()
        return file

    def sources_of(self, where: str) -> set[uuid.UUID]:
        return {self.drive_source.id if where == "drive" else self.local_source.id}

    def connector(self, where: str):  # noqa: ANN201
        return self.drive if where == "drive" else self.local

    def ask(self, question: str, where: str, conversation_id=None, page=None):  # noqa: ANN001, ANN201
        return ConversationService(self.db, ai_gateway=_gateway()).ask(
            organization_id=self.org.id,
            user_id=self.user.id,
            conversation_id=conversation_id,
            question=question,
            workspace=self.connector(where),
            page=page,
        )

    def analyze(self) -> None:
        job = StorageAnalysisJobRepository(self.db).create(
            organization_id=self.org.id,
            triggered_by=StorageAnalysisTrigger.MANUAL,
            triggered_by_user_id=None,
        )
        self.db.commit()
        StorageIntelligenceService(self.db).run(job.id)
        self.db.expire_all()

    def plans(self) -> ExecutionPlanService:
        return ExecutionPlanService(self.db, enqueue_execution_job=lambda _job_id: None)


@pytest.fixture
def both(db: Session) -> _TwoWorkspaces:
    return _TwoWorkspaces(db)


def _source_ids(files) -> set[uuid.UUID]:  # noqa: ANN001
    return {file.storage_source_id for file in files}


def _file_ids_in(turn) -> set[str]:  # noqa: ANN001
    return {
        item["id"] for block in turn.assistant_message.blocks for item in block.get("items", [])
    }


# -- search ---------------------------------------------------------------------------


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_search_by_name_returns_only_the_workspaces_files(both: _TwoWorkspaces, where: str) -> None:
    both.file("drive", "/Finance/Blarrow invoice.pdf")
    both.file("local", "/Finance/Blarrow invoice.pdf")

    outcome = SearchService(both.db, ai_gateway=_gateway()).find(
        "Blarrow invoice",
        organization_id=both.org.id,
        user_id=both.user.id,
        filters=FileQuery(connector_id=str(both.connector(where).id)),
    )

    assert outcome.total == 1
    assert _source_ids(r.file for r in outcome.results) == both.sources_of(where)


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_content_and_similarity_search_cannot_reach_the_other_storage(
    both: _TwoWorkspaces, where: str
) -> None:
    # Named nothing like the query: found only through content or similarity.
    both.file("drive", "/scan001.pdf", text=_INVOICE)
    both.file("local", "/scan001.pdf", text=_INVOICE)

    outcome = SearchService(both.db, ai_gateway=_gateway()).find(
        "payment due Blarrow",
        organization_id=both.org.id,
        user_id=both.user.id,
        filters=FileQuery(connector_id=str(both.connector(where).id)),
        allow_ai=False,
    )

    assert outcome.results
    assert _source_ids(r.file for r in outcome.results) == both.sources_of(where)


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_grounding_retrieval_stays_in_the_workspace(both: _TwoWorkspaces, where: str) -> None:
    both.file("drive", "/scan001.pdf", text=_INVOICE)
    both.file("local", "/scan001.pdf", text=_INVOICE)

    results = SearchService(both.db, ai_gateway=_gateway()).search(
        "When is the Blarrow payment due?",
        organization_id=both.org.id,
        user_id=both.user.id,
        connector_id=both.connector(where).id,
    )

    assert results
    assert _source_ids(r.file for r in results) == both.sources_of(where)


# -- Ask Vault ------------------------------------------------------------------------


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_ask_vault_lists_only_the_workspaces_files(both: _TwoWorkspaces, where: str) -> None:
    drive = both.file("drive", "/Finance/Blarrow invoice.pdf")
    local = both.file("local", "/Finance/Blarrow invoice.pdf")

    turn = both.ask("Find all Blarrow files", where)

    expected = drive if where == "drive" else local
    assert _file_ids_in(turn) == {str(expected.id)}
    assert turn.conversation.connector_id == both.connector(where).id


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_ask_vault_citations_come_from_the_workspace(both: _TwoWorkspaces, where: str) -> None:
    both.file("drive", "/scan001.pdf", text=_INVOICE)
    both.file("local", "/scan001.pdf", text=_INVOICE)

    turn = both.ask("When is the Blarrow payment due?", where)

    cited = FileRepository(both.db).list_by_ids([c.file_id for c in turn.citations])
    assert cited
    assert _source_ids(cited) == both.sources_of(where)


@requires_infra
def test_asking_for_the_other_storage_says_where_you_are(both: _TwoWorkspaces) -> None:
    both.file("local", "/Clips/holiday.mp4")

    turn = both.ask("Show the largest files on my computer", "drive")

    assert "separate workspace" in turn.assistant_message.content
    assert _file_ids_in(turn) == set()


@requires_infra
def test_a_conversation_cannot_continue_in_another_workspace(both: _TwoWorkspaces) -> None:
    both.file("drive", "/Finance/Blarrow invoice.pdf")
    first = both.ask("Find all Blarrow files", "drive")

    with pytest.raises(ConflictError):
        both.ask("Trash them", "local", conversation_id=first.conversation.id)


@requires_infra
def test_files_on_screen_from_another_storage_are_never_carried_over(both: _TwoWorkspaces) -> None:
    drive_file = both.file("drive", "/Finance/Blarrow invoice.pdf")
    both.file("local", "/Notes/todo.txt")

    turn = both.ask(
        "Move these into a folder called Finance",
        "local",
        page=PageContext(page="files", file_ids=(str(drive_file.id),)),
    )

    assert str(drive_file.id) not in _file_ids_in(turn)
    assert all(block["type"] != "action_proposal" for block in turn.assistant_message.blocks)


@requires_infra
def test_files_on_screen_in_the_workspace_become_these(both: _TwoWorkspaces) -> None:
    old = both.file("drive", "/Big/raw-footage.pdf")
    both.file("drive", "/Big/other.pdf")

    turn = both.ask(
        "Archive these",
        "drive",
        page=PageContext(page="large-files", label="Large files", file_ids=(str(old.id),)),
    )

    proposal = next(b for b in turn.assistant_message.blocks if b["type"] == "action_proposal")
    assert [item["id"] for item in proposal["items"]] == [str(old.id)]


@requires_infra
def test_a_local_archive_proposal_holds_only_local_files(both: _TwoWorkspaces) -> None:
    local_file = both.file("local", "/Big/raw-footage.pdf")
    drive_file = both.file("drive", "/Big/raw-footage.pdf")

    turn = both.ask(
        "Archive these",
        "local",
        page=PageContext(page="large-files", file_ids=(str(local_file.id), str(drive_file.id))),
    )

    proposal = next(b for b in turn.assistant_message.blocks if b["type"] == "action_proposal")
    assert [item["id"] for item in proposal["items"]] == [str(local_file.id)]


# -- confirming and planning -------------------------------------------------------------


class _RecordingPlans:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_ad_hoc_plan(self, file_ids, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append({"file_ids": list(file_ids), **kwargs})

        class _Plan:
            id = uuid.uuid4()

        return _Plan()


@requires_infra
def test_a_proposal_is_never_confirmed_from_another_workspace(both: _TwoWorkspaces) -> None:
    both.file("drive", "/Finance/Blarrow invoice.pdf")
    first = both.ask("Find all Blarrow files", "drive")
    turn = both.ask("Trash them", "drive", conversation_id=first.conversation.id)
    action_id = next(
        b["action_id"] for b in turn.assistant_message.blocks if b["type"] == "action_proposal"
    )
    plans = _RecordingPlans()

    with pytest.raises(ConflictError):
        VaultActionService(both.db, plans=plans).confirm(
            turn.conversation.id,
            action_id,
            organization_id=both.org.id,
            user_id=both.user.id,
            workspace_id=both.local.id,
        )
    assert plans.calls == []


@requires_infra
def test_a_tampered_proposal_never_reaches_the_other_storages_files(both: _TwoWorkspaces) -> None:
    drive_file = both.file("drive", "/Finance/Blarrow invoice.pdf")
    local_file = both.file("local", "/Finance/Blarrow invoice.pdf")
    first = both.ask("Find all Blarrow files", "drive")
    turn = both.ask("Trash them", "drive", conversation_id=first.conversation.id)
    conversation = turn.conversation
    action_id, proposal = next(iter(conversation.state["pending"].items()))
    conversation.state = {
        **conversation.state,
        "pending": {action_id: {**proposal, "file_ids": [str(drive_file.id), str(local_file.id)]}},
    }
    both.db.commit()
    plans = _RecordingPlans()

    VaultActionService(both.db, plans=plans).confirm(
        conversation.id,
        action_id,
        organization_id=both.org.id,
        user_id=both.user.id,
        workspace_id=both.drive.id,
    )

    assert [call["file_ids"] for call in plans.calls] == [[drive_file.id]]
    assert plans.calls[0]["connector_id"] == both.drive.id


@requires_infra
@pytest.mark.parametrize("where", ["drive", "local"])
def test_a_plan_refuses_files_from_outside_its_workspace(both: _TwoWorkspaces, where: str) -> None:
    other = both.file("local" if where == "drive" else "drive", "/Notes/todo.txt")

    with pytest.raises(ValidationError, match="different storage"):
        both.plans().create_ad_hoc_plan(
            [other.id],
            action_type=ExecutionActionType.ARCHIVE,
            organization_id=both.org.id,
            user_id=both.user.id,
            require_approval=False,
            connector_id=both.connector(where).id,
        )


# -- duplicates, statistics, workspace identity ----------------------------------------------


@requires_infra
def test_identical_files_in_two_storages_are_never_one_duplicate_group(
    both: _TwoWorkspaces,
) -> None:
    both.file("drive", "/Photos/beach.pdf", checksum="same-bytes")
    both.file("local", "/Photos/beach.pdf", checksum="same-bytes")

    both.analyze()

    groups, total = DuplicateGroupRepository(both.db).list_for_organization(
        both.org.id, limit=10, offset=0
    )
    assert total == 0 and groups == []


@requires_infra
def test_duplicates_are_grouped_and_planned_within_their_own_storage(both: _TwoWorkspaces) -> None:
    both.file("drive", "/Photos/beach.pdf", checksum="drive-dup")
    both.file("drive", "/Backup/beach.pdf", checksum="drive-dup")
    both.file("local", "/Photos/beach.pdf", checksum="local-dup")
    both.file("local", "/Backup/beach.pdf", checksum="local-dup")
    both.analyze()
    repo = DuplicateGroupRepository(both.db)

    for where in ("drive", "local"):
        [group], _ = repo.list_for_organization(
            both.org.id, limit=10, offset=0, connector_id=both.connector(where).id
        )
        members = repo.list_members_with_files(group.id)
        assert _source_ids(file for _m, file in members) == both.sources_of(where)

    [drive_group], _ = repo.list_for_organization(
        both.org.id, limit=10, offset=0, connector_id=both.drive.id
    )
    with pytest.raises(ValidationError, match="different storage"):
        both.plans().create_plan_from_duplicate_group(
            drive_group.id,
            organization_id=both.org.id,
            user_id=both.user.id,
            require_approval=False,
            connector_id=both.local.id,
        )


@requires_infra
def test_dashboard_activity_is_per_workspace(both: _TwoWorkspaces) -> None:
    drive_file = both.file("drive", "/Docs/plan.pdf")
    local_file = both.file("local", "/Docs/plan.pdf")
    service = DashboardService(both.db)

    drive_view = service.get_overview(both.org.id, connector_id=both.drive.id)
    local_view = service.get_overview(both.org.id, connector_id=both.local.id)

    assert [f.id for f in drive_view.recent_activity] == [drive_file.id]
    assert [f.id for f in local_view.recent_activity] == [local_file.id]


@requires_infra
def test_storage_statistics_are_computed_per_workspace(both: _TwoWorkspaces) -> None:
    both.file("drive", "/a.pdf")
    both.file("drive", "/b.pdf")
    both.file("local", "/c.pdf")
    both.analyze()

    snapshots = StorageAnalysisSnapshotRepository(both.db)
    drive = snapshots.get_latest_for_organization(both.org.id, connector_id=both.drive.id)
    local = snapshots.get_latest_for_organization(both.org.id, connector_id=both.local.id)

    assert (drive.total_files, local.total_files) == (2, 1)


@requires_infra
def test_another_organizations_storage_is_not_a_workspace(
    both: _TwoWorkspaces, db: Session
) -> None:
    stranger = _TwoWorkspaces(db)

    with pytest.raises(NotFoundError):
        resolve_workspace(db, connector_id=stranger.drive.id, organization_id=both.org.id)
