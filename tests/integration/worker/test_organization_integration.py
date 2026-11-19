"""Phase 2 — Organization Intelligence, end to end against a real Postgres and
a fake Google Drive client (ADR-020: never real Drive, never a real LLM).

Covers: entity inference through `AIGateway.recommend()`, the deterministic
lifecycle/recommendation passes, AI-outage degradation, applying a
recommendation through the real Execution Engine (folder creation + MOVE_FILE
plan, verified against the fake provider), the organizational-memory
correction hook, tenant isolation of folder creation, and near-duplicate
discovery."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_execution_service_integration import (
    _FakeGoogleDriveClient,
    _FakeGoogleWorkspaceOAuthClient,
    _FakeObjectStorageClient,
    _provision_connector,
    _provision_user,
    requires_infra,
)

from vault_shared import NotFoundError
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.interfaces import CompletionResult, Message
from vault_shared.ai_gateway.providers import (
    ExtractiveCompletionProvider,
    LocalEmbeddingProvider,
)
from vault_shared.connectors.google_drive import DriveFile
from vault_shared.db.models import (
    DriveType,
    EntityLinkSource,
    ExecutionActionType,
    ExecutionJobStatus,
    ExtractionStatus,
    FileLifecycleState,
    IntelligenceStatus,
    MemoryType,
    OrganizationAnalysisJobStatus,
    OrganizationRecommendationKind,
    OrganizationRecommendationStatus,
    RelationshipType,
    VerificationStatus,
)
from vault_shared.db.repositories import (
    EmbeddingRepository,
    ExecutionJobRepository,
    ExecutionResultRepository,
    FileEntityLinkRepository,
    FileExtractionRepository,
    FileIntelligenceRepository,
    FileLifecycleRepository,
    FileRelationshipRepository,
    FileRepository,
    FolderRepository,
    OrganizationAnalysisJobRepository,
    OrganizationEntityRepository,
    OrganizationMemoryRepository,
    OrganizationRecommendationRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import DRIVE_WRITE_SCOPE
from vault_shared.execution.plan_service import ExecutionPlanService
from vault_shared.storage.default_registry import build_storage_registry
from worker.execution.execution_service import ExecutionService
from worker.organization import (
    organization_recommendation_apply_service as apply_module,
)
from worker.organization.organization_analysis_service import (
    OrganizationAnalysisService,
)
from worker.organization.organization_recommendation_apply_service import (
    OrganizationRecommendationApplyService,
)
from worker.organization.organization_recommendation_generator import (
    OrganizationRecommendationGenerator,
)
from worker.relationships.near_duplicate_service import NearDuplicateService


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _ScriptedCompletionProvider:
    name = "scripted_completion_provider"
    model_name = "scripted-model"

    def __init__(self, response: dict | str) -> None:
        self._text = response if isinstance(response, str) else json.dumps(response)
        self.calls: list[list[Message]] = []

    def complete(
        self, *, messages: list[Message], context: str | None, max_tokens: int
    ) -> CompletionResult:
        self.calls.append(messages)
        return CompletionResult(
            text=self._text, provider=self.name, model_name=self.model_name, tokens_used=10
        )


class _DriveWithFolders(_FakeGoogleDriveClient):
    """`_FakeGoogleDriveClient` plus `create_folder` — the one Drive call
    Phase 2 adds to the Execution Engine's repertoire."""

    def create_folder(self, *, access_token: str, name: str, parent_id: str | None) -> DriveFile:
        folder_id = f"folder-{uuid.uuid4().hex[:8]}"
        self.calls.append(("create_folder", folder_id))
        now = datetime.now(UTC)
        folder = DriveFile(
            id=folder_id,
            name=name,
            mime_type="application/vnd.google-apps.folder",
            parents=[parent_id or "root"],
            size=None,
            created_time=now,
            modified_time=now,
            viewed_by_me_time=None,
            owner_email="founder@acme.com",
            shared=False,
            checksum=None,
            version_id=None,
            is_folder=True,
            trashed=False,
        )
        self._files[folder_id] = folder
        return folder

    def parents_of(self, file_id: str) -> list[str]:
        return list(self._files[file_id].parents)


def _gateway(completion_provider: object) -> AIGateway:
    return AIGateway(
        embedding_provider=LocalEmbeddingProvider(),
        completion_provider=completion_provider,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
    )


def _drive_file(file_id: str, name: str, parent: str) -> DriveFile:
    now = datetime.now(UTC)
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="text/plain",
        parents=[parent],
        size=100,
        created_time=now,
        modified_time=now,
        viewed_by_me_time=None,
        owner_email="founder@acme.com",
        shared=False,
        checksum=None,
        version_id=None,
        is_folder=False,
        trashed=False,
    )


class _Workspace:
    """One organization with two scattered folders holding two related
    files — the minimal "messy Drive" this feature exists for."""

    def __init__(self, db: Session) -> None:
        self.user = _provision_user(db)
        self.connector = _provision_connector(
            db,
            organization_id=self.user.organization_id,
            user_id=self.user.id,
            granted_scopes=DRIVE_WRITE_SCOPE,
        )
        self.source = StorageSourceRepository(db).upsert(
            connector_id=self.connector.id,
            provider_drive_id="root",
            name="My Drive",
            drive_type=DriveType.MY_DRIVE,
        )
        self.folder_a = self._folder(db, "drv-folder-a", "Downloads")
        self.folder_b = self._folder(db, "drv-folder-b", "Desktop Stuff")
        self.file_a = self._file(db, "drv-a", "Acme Rebrand Brief.docx", self.folder_a)
        self.file_b = self._file(db, "drv-b", "Acme Rebrand Brief_v2.docx", self.folder_b)
        db.commit()

    @property
    def organization_id(self) -> uuid.UUID:
        return self.user.organization_id

    def _folder(self, db: Session, provider_id: str, name: str):
        return FolderRepository(db).upsert(
            storage_source_id=self.source.id,
            provider_file_id=provider_id,
            provider_parent_id=None,
            parent_folder_id=None,
            name=name,
            path=f"/{name}",
            owner_email="founder@acme.com",
            is_shared=False,
            provider_created_at=None,
            provider_modified_at=None,
            scanned_at=datetime.now(UTC),
        )

    def _file(self, db: Session, provider_id: str, name: str, folder, *, checksum=None):
        now = datetime.now(UTC)
        file = FileRepository(db).upsert(
            storage_source_id=self.source.id,
            provider_file_id=provider_id,
            provider_parent_id=folder.provider_file_id,
            parent_folder_id=folder.id,
            name=name,
            path=f"{folder.path}/{name}",
            mime_type="text/plain",
            size_bytes=100,
            owner_email="founder@acme.com",
            is_shared=False,
            permissions_summary=None,
            version_id=None,
            checksum=checksum,
            web_view_link=None,
            provider_created_at=now - timedelta(days=5),
            provider_modified_at=now - timedelta(days=5),
            provider_viewed_at=now - timedelta(days=1),
            scanned_at=now,
        )
        FileExtractionRepository(db).upsert(
            file_id=file.id,
            status=ExtractionStatus.SUCCESS,
            extractor_name="plain_text",
            extracted_text=f"Brief for the Acme rebrand project. ({name})",
            char_count=40,
            error=None,
            extracted_at=now,
        )
        return file

    def drive(self) -> _DriveWithFolders:
        return _DriveWithFolders(
            files={
                "drv-a": _drive_file("drv-a", self.file_a.name, "drv-folder-a"),
                "drv-b": _drive_file("drv-b", self.file_b.name, "drv-folder-b"),
            }
        )


def _classify_everything(project: str, *, confidence: float = 0.9) -> dict:
    return {
        "intent": "classify",
        "recommendations": [
            {
                "kind": "classify",
                "target_ref": ref,
                "labels": {"project": project},
                "reason": "Both files are briefs for the same rebrand.",
                "confidence": confidence,
                "evidence": ["Both file names start with 'Acme Rebrand Brief'."],
            }
            for ref in ("f0", "f1")
        ],
        "reasoning_summary": "Same project.",
        "confidence": confidence,
        "evidence": [],
        "unknowns": [],
    }


def _analyze(db: Session, organization_id: uuid.UUID, provider: object):
    job = OrganizationAnalysisJobRepository(db).create(
        organization_id=organization_id, triggered_by_user_id=None
    )
    db.commit()
    OrganizationAnalysisService(db, ai_gateway=_gateway(provider)).run(job.id)
    db.expire_all()
    return OrganizationAnalysisJobRepository(db).get_by_id(job.id)


def _execution_service(db: Session, drive: _FakeGoogleDriveClient) -> ExecutionService:
    return ExecutionService(
        db,
        storage=build_storage_registry(
            db, oauth_client=_FakeGoogleWorkspaceOAuthClient(), drive_client=drive
        ),
        object_storage_client=_FakeObjectStorageClient(),
    )


@requires_infra
def test_analysis_infers_an_entity_scores_lifecycle_and_proposes_consolidation(
    db: Session,
) -> None:
    workspace = _Workspace(db)
    provider = _ScriptedCompletionProvider(_classify_everything("Acme Rebrand"))

    job = _analyze(db, workspace.organization_id, provider)

    assert job.status == OrganizationAnalysisJobStatus.COMPLETED
    assert job.clusters_found == 1
    assert job.entities_created == 1
    assert len(provider.calls) == 1

    [entity] = OrganizationEntityRepository(db).list_for_organization(workspace.organization_id)
    assert (entity.entity_type, entity.name) == ("project", "Acme Rebrand")
    assert entity.evidence[0]["type"] == "ai_inference"

    links = FileEntityLinkRepository(db).list_for_entity(entity.id)
    assert {link.file_id for link in links} == {workspace.file_a.id, workspace.file_b.id}
    assert all(link.source == EntityLinkSource.AI_INFERRED for link in links)

    lifecycle = FileLifecycleRepository(db).get_by_file_id(workspace.file_a.id)
    assert lifecycle.state == FileLifecycleState.ACTIVE
    assert lifecycle.evidence

    [recommendation] = OrganizationRecommendationRepository(db).list_for_organization(
        workspace.organization_id
    )
    assert recommendation.kind == OrganizationRecommendationKind.GROUP_PROJECT
    assert recommendation.suggested_destination == ["Projects", "Acme Rebrand"]
    assert {loc["path"] for loc in recommendation.current_locations} == {
        "/Downloads",
        "/Desktop Stuff",
    }
    assert recommendation.evidence[0]["type"] == "scattered_locations"


@requires_infra
def test_file_content_reaches_the_model_only_inside_the_untrusted_wrapper(db: Session) -> None:
    workspace = _Workspace(db)
    provider = _ScriptedCompletionProvider(_classify_everything("Acme Rebrand"))

    _analyze(db, workspace.organization_id, provider)

    [messages] = provider.calls
    system = next(m for m in messages if m.role == "system")
    user = next(m for m in messages if m.role == "user")
    assert "Acme Rebrand Brief" not in system.content
    assert "<untrusted_data" in user.content
    assert "Acme Rebrand Brief" in user.content
    # Database identifiers never leave AI Vault — only short refs do.
    assert str(workspace.file_a.id) not in user.content


@requires_infra
def test_rerunning_analysis_does_not_call_the_model_again_for_linked_files(db: Session) -> None:
    workspace = _Workspace(db)
    provider = _ScriptedCompletionProvider(_classify_everything("Acme Rebrand"))

    _analyze(db, workspace.organization_id, provider)
    _analyze(db, workspace.organization_id, provider)

    assert len(provider.calls) == 1
    assert (
        len(OrganizationEntityRepository(db).list_for_organization(workspace.organization_id)) == 1
    )


@requires_infra
def test_a_low_confidence_label_leaves_the_file_unlinked(db: Session) -> None:
    workspace = _Workspace(db)
    provider = _ScriptedCompletionProvider(_classify_everything("Maybe Acme", confidence=0.2))

    job = _analyze(db, workspace.organization_id, provider)

    assert job.status == OrganizationAnalysisJobStatus.COMPLETED
    assert OrganizationEntityRepository(db).list_for_organization(workspace.organization_id) == []
    assert FileEntityLinkRepository(db).list_for_file(workspace.file_a.id) == []


@requires_infra
def test_an_invented_ref_from_the_model_is_never_linked(db: Session) -> None:
    workspace = _Workspace(db)
    response = _classify_everything("Acme Rebrand")
    for action in response["recommendations"]:
        action["target_ref"] = "f99"
    provider = _ScriptedCompletionProvider(response)

    _analyze(db, workspace.organization_id, provider)

    assert OrganizationEntityRepository(db).list_for_organization(workspace.organization_id) == []


@requires_infra
def test_with_ai_disabled_the_deterministic_passes_still_complete(db: Session) -> None:
    workspace = _Workspace(db)

    job = _analyze(db, workspace.organization_id, ExtractiveCompletionProvider())

    assert job.status == OrganizationAnalysisJobStatus.COMPLETED
    assert job.entities_created == 0
    assert OrganizationEntityRepository(db).list_for_organization(workspace.organization_id) == []
    lifecycle = FileLifecycleRepository(db).get_by_file_id(workspace.file_a.id)
    assert lifecycle is not None
    assert lifecycle.state == FileLifecycleState.KEEP


@requires_infra
def test_an_unusable_model_response_degrades_without_failing_the_job(db: Session) -> None:
    workspace = _Workspace(db)

    job = _analyze(db, workspace.organization_id, _ScriptedCompletionProvider("not json at all"))

    assert job.status == OrganizationAnalysisJobStatus.COMPLETED
    assert FileLifecycleRepository(db).get_by_file_id(workspace.file_b.id) is not None


@requires_infra
def test_applying_a_recommendation_creates_the_folder_and_really_moves_the_files(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(db)
    _analyze(
        db,
        workspace.organization_id,
        _ScriptedCompletionProvider(_classify_everything("Acme Rebrand")),
    )
    [recommendation] = OrganizationRecommendationRepository(db).list_for_organization(
        workspace.organization_id
    )
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(apply_module, "_enqueue_execution_job", enqueued.append)
    drive = workspace.drive()
    execution_service = _execution_service(db, drive)

    plan_id = OrganizationRecommendationApplyService(db, execution_service=execution_service).apply(
        recommendation.id, organization_id=workspace.organization_id, user_id=workspace.user.id
    )

    db.expire_all()
    applied = OrganizationRecommendationRepository(db).get_owned(
        recommendation.id, organization_id=workspace.organization_id
    )
    assert applied.status == OrganizationRecommendationStatus.APPLIED
    assert applied.execution_plan_id == plan_id

    projects = FolderRepository(db).get_by_parent_and_name(
        storage_source_id=workspace.source.id, parent_folder_id=None, name="Projects"
    )
    destination = FolderRepository(db).get_by_parent_and_name(
        storage_source_id=workspace.source.id, parent_folder_id=projects.id, name="Acme Rebrand"
    )
    assert destination.path == "/Projects/Acme Rebrand"
    assert [call[0] for call in drive.calls].count("create_folder") == 2

    [job_id] = enqueued
    execution_service.run(job_id)
    db.expire_all()

    assert ExecutionJobRepository(db).get_by_id(job_id).status == ExecutionJobStatus.COMPLETED
    assert drive.parents_of("drv-a") == [destination.provider_file_id]
    assert drive.parents_of("drv-b") == [destination.provider_file_id]
    results = ExecutionResultRepository(db).list_for_job(job_id)
    assert results
    assert all(r.verification_status == VerificationStatus.VERIFIED for r in results)


@requires_infra
def test_an_existing_destination_folder_is_reused_not_duplicated(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(db)
    _analyze(
        db,
        workspace.organization_id,
        _ScriptedCompletionProvider(_classify_everything("Acme Rebrand")),
    )
    [recommendation] = OrganizationRecommendationRepository(db).list_for_organization(
        workspace.organization_id
    )
    projects = workspace._folder(db, "drv-projects", "Projects")
    db.commit()
    monkeypatch.setattr(apply_module, "_enqueue_execution_job", lambda _job_id: None)
    drive = workspace.drive()

    OrganizationRecommendationApplyService(
        db, execution_service=_execution_service(db, drive)
    ).apply(recommendation.id, organization_id=workspace.organization_id, user_id=workspace.user.id)

    assert [call[0] for call in drive.calls].count("create_folder") == 1
    created = FolderRepository(db).get_by_parent_and_name(
        storage_source_id=workspace.source.id, parent_folder_id=projects.id, name="Acme Rebrand"
    )
    assert created is not None


@requires_infra
def test_create_folder_refuses_another_organizations_storage_source(db: Session) -> None:
    owner = _Workspace(db)
    intruder = _Workspace(db)
    drive = owner.drive()

    with pytest.raises(NotFoundError):
        _execution_service(db, drive).create_folder(
            organization_id=intruder.organization_id,
            storage_source_id=owner.source.id,
            name="Sneaky",
        )

    assert drive.calls == []


@requires_infra
def test_moving_a_file_elsewhere_than_recommended_is_remembered_as_a_correction(
    db: Session,
) -> None:
    workspace = _Workspace(db)
    _analyze(
        db,
        workspace.organization_id,
        _ScriptedCompletionProvider(_classify_everything("Acme Rebrand")),
    )
    [recommendation] = OrganizationRecommendationRepository(db).list_for_organization(
        workspace.organization_id
    )

    ExecutionPlanService(db).create_ad_hoc_plan(
        [workspace.file_a.id],
        action_type=ExecutionActionType.MOVE_FILE,
        organization_id=workspace.organization_id,
        user_id=workspace.user.id,
        new_parent_id="drv-folder-b",
    )

    [memory] = OrganizationMemoryRepository(db).list_for_organization(
        workspace.organization_id, memory_type=MemoryType.CORRECTION
    )
    assert memory.key == f"entity:{recommendation.entity_id}"
    assert memory.value["actual_new_parent_id"] == "drv-folder-b"
    assert memory.confidence == 1.0


@requires_infra
def test_moving_a_file_exactly_where_recommended_records_no_correction(db: Session) -> None:
    workspace = _Workspace(db)
    _analyze(
        db,
        workspace.organization_id,
        _ScriptedCompletionProvider(_classify_everything("Acme Rebrand")),
    )
    projects = workspace._folder(db, "drv-projects", "Projects")
    target = FolderRepository(db).upsert(
        storage_source_id=workspace.source.id,
        provider_file_id="drv-acme",
        provider_parent_id="drv-projects",
        parent_folder_id=projects.id,
        name="Acme Rebrand",
        path="/Projects/Acme Rebrand",
        owner_email="founder@acme.com",
        is_shared=False,
        provider_created_at=None,
        provider_modified_at=None,
        scanned_at=datetime.now(UTC),
    )
    db.commit()

    ExecutionPlanService(db).create_ad_hoc_plan(
        [workspace.file_a.id],
        action_type=ExecutionActionType.MOVE_FILE,
        organization_id=workspace.organization_id,
        user_id=workspace.user.id,
        new_parent_id=target.provider_file_id,
    )

    assert OrganizationMemoryRepository(db).list_for_organization(workspace.organization_id) == []


@requires_infra
def test_recorded_corrections_are_fed_back_to_the_model_as_constraints(db: Session) -> None:
    workspace = _Workspace(db)
    _analyze(
        db,
        workspace.organization_id,
        _ScriptedCompletionProvider(_classify_everything("Acme Rebrand")),
    )
    ExecutionPlanService(db).create_ad_hoc_plan(
        [workspace.file_a.id],
        action_type=ExecutionActionType.MOVE_FILE,
        organization_id=workspace.organization_id,
        user_id=workspace.user.id,
        new_parent_id="drv-folder-b",
    )
    newcomer = workspace._file(db, "drv-c", "Acme Rebrand Brief_v3.docx", workspace.folder_a)
    db.commit()
    provider = _ScriptedCompletionProvider(_classify_everything("Acme Rebrand"))

    _analyze(db, workspace.organization_id, provider)

    assert newcomer is not None
    [messages] = provider.calls
    user = next(m for m in messages if m.role == "user")
    assert "different location than this active organization recommendation" in user.content
    assert "Acme Rebrand" in user.content  # known entity offered for reuse


def _embed(db: Session, file_id: uuid.UUID, vector: list[float]) -> None:
    EmbeddingRepository(db).upsert(
        file_id=file_id,
        model_name="test",
        model_version="1",
        dimensions=len(vector),
        vector=vector,
        content_hash=uuid.uuid4().hex,
        embedded_at=datetime.now(UTC),
    )


@requires_infra
def test_near_duplicates_are_linked_but_unrelated_and_exact_duplicates_are_not(
    db: Session,
) -> None:
    workspace = _Workspace(db)
    unrelated = workspace._file(db, "drv-z", "Quarterly Taxes.xlsx", workspace.folder_a)
    exact_copy = workspace._file(
        db, "drv-a-copy", "Acme Rebrand Brief (1).docx", workspace.folder_b, checksum="same"
    )
    workspace.file_a.checksum = "same"
    _embed(db, workspace.file_a.id, [1.0, 0.0, 0.0, 0.0])
    _embed(db, workspace.file_b.id, [0.98, 0.05, 0.0, 0.0])
    _embed(db, exact_copy.id, [1.0, 0.0, 0.0, 0.0])
    _embed(db, unrelated.id, [0.0, 0.0, 1.0, 0.0])
    db.commit()

    NearDuplicateService(db).discover_for_connector(workspace.connector.id)
    db.commit()

    edges = {
        frozenset((r.file_id, r.related_file_id))
        for r in FileRelationshipRepository(db).list_for_organization(workspace.organization_id)
        if r.relationship_type == RelationshipType.NEAR_DUPLICATE
    }
    assert frozenset((workspace.file_a.id, workspace.file_b.id)) in edges
    assert frozenset((workspace.file_a.id, exact_copy.id)) not in edges
    assert not any(unrelated.id in edge for edge in edges)


def _untitled_file_with_evidence(db: Session, workspace: _Workspace, *, linked: bool = True):
    untitled = workspace._file(db, "drv-untitled", "Untitled.docx", workspace.folder_a)
    FileIntelligenceRepository(db).upsert(
        file_id=untitled.id,
        status=IntelligenceStatus.SUCCESS,
        document_type="meeting notes",
        summary="Kickoff notes for the Acme rebrand.",
        entities=[],
        structured_metadata={},
        topics=[],
        confidence=0.8,
        provider="test",
        model_name="test",
        error=None,
        processed_at=datetime.now(UTC),
    )
    if linked:
        entity = OrganizationEntityRepository(db).upsert(
            organization_id=workspace.organization_id,
            entity_type="project",
            name="Acme Rebrand",
            normalized_name="acme rebrand",
            confidence=0.9,
            evidence=[],
        )
        FileEntityLinkRepository(db).upsert(
            file_id=untitled.id,
            entity_id=entity.id,
            confidence=0.9,
            evidence=[],
            source=EntityLinkSource.AI_INFERRED,
            inference_version="test",
        )
    db.commit()
    return untitled


def _rename_recommendations(db: Session, organization_id: uuid.UUID):
    return OrganizationRecommendationRepository(db).list_for_organization(
        organization_id, kind=OrganizationRecommendationKind.RENAME_FILE
    )


@requires_infra
def test_an_uninformative_name_gets_an_evidence_based_rename_proposal(db: Session) -> None:
    workspace = _Workspace(db)
    untitled = _untitled_file_with_evidence(db, workspace)

    OrganizationRecommendationGenerator(db).generate_for_organization(workspace.organization_id)

    [recommendation] = _rename_recommendations(db, workspace.organization_id)
    month = untitled.provider_modified_at.strftime("%Y-%m")
    assert recommendation.suggested_destination == [f"Acme Rebrand - Meeting Notes - {month}.docx"]
    assert recommendation.affected_file_ids == [str(untitled.id)]
    assert {item["type"] for item in recommendation.evidence} == {
        "uninformative_name",
        "entity",
        "content",
    }


@requires_infra
def test_descriptive_names_get_no_rename_proposal(db: Session) -> None:
    workspace = _Workspace(db)

    OrganizationRecommendationGenerator(db).generate_for_organization(workspace.organization_id)

    assert _rename_recommendations(db, workspace.organization_id) == []


@requires_infra
def test_regenerating_refreshes_a_rename_proposal_instead_of_duplicating_it(db: Session) -> None:
    workspace = _Workspace(db)
    _untitled_file_with_evidence(db, workspace)
    generator = OrganizationRecommendationGenerator(db)

    generator.generate_for_organization(workspace.organization_id)
    generator.generate_for_organization(workspace.organization_id)

    assert len(_rename_recommendations(db, workspace.organization_id)) == 1


@requires_infra
def test_applying_a_rename_proposal_really_renames_the_file(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _Workspace(db)
    untitled = _untitled_file_with_evidence(db, workspace)
    OrganizationRecommendationGenerator(db).generate_for_organization(workspace.organization_id)
    [recommendation] = _rename_recommendations(db, workspace.organization_id)
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(apply_module, "_enqueue_execution_job", enqueued.append)
    drive = workspace.drive()
    drive._files["drv-untitled"] = _drive_file("drv-untitled", "Untitled.docx", "drv-folder-a")
    execution_service = _execution_service(db, drive)

    OrganizationRecommendationApplyService(db, execution_service=execution_service).apply(
        recommendation.id, organization_id=workspace.organization_id, user_id=workspace.user.id
    )
    [job_id] = enqueued
    execution_service.run(job_id)
    db.expire_all()

    assert drive._files["drv-untitled"].name == recommendation.suggested_destination[0]
    assert FileRepository(db).get_by_id(untitled.id).name == recommendation.suggested_destination[0]
