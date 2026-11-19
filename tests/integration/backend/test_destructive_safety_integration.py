"""Gate 1: the incident that trashed a project's `__init__.py` files must not
be repeatable, from any entry point.

What happened: "delete duplicate" in Ask Vault proposed every non-kept copy
of every duplicate group on the computer — including empty `__init__.py`
files (all empty files share one checksum), `tsconfig.json`s and driver
files — and one confirmation moved 616 files to the agent's Trash. These
tests pin each guard that now stands in the way, against a real database
and disposable data only; nothing touches real storage.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_vault_assistant_integration import (
    _block,
    _ScriptedModel,
    _Storage,
    requires_infra,
)

from app.application.action_service import ActionService
from vault_shared import ValidationError
from vault_shared.db.models import (
    ExecutionActionType,
    ExecutionResultStatus,
    ExecutionStepStatus,
    VerificationStatus,
)
from vault_shared.db.repositories import (
    DuplicateGroupRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
    FolderRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.execution import ExecutionPlanService
from vault_shared.execution.plan_service import permanent_delete_confirmation
from vault_shared.protection import AssetProtection


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _Disk(_Storage):
    """A computer with a real-looking project on it, plus ordinary files."""

    def at(self, path: str, *, size: int = 1000, checksum: str | None = None):  # noqa: ANN201
        name = path.rsplit("/", 1)[-1]
        now = datetime.now(UTC)
        file = FileRepository(self.db).upsert(
            storage_source_id=self.source.id,
            provider_file_id=f"f-{uuid.uuid4().hex[:10]}",
            provider_parent_id=None,
            parent_folder_id=None,
            name=name,
            path=path,
            mime_type=None,
            size_bytes=size,
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
        self.db.commit()
        return file

    def git_folder(self, path: str) -> None:
        FolderRepository(self.db).upsert(
            storage_source_id=self.source.id,
            provider_file_id=f"d-{uuid.uuid4().hex[:10]}",
            provider_parent_id=None,
            parent_folder_id=None,
            name=path.rsplit("/", 1)[-1],
            path=path,
            owner_email=None,
            is_shared=False,
            provider_created_at=None,
            provider_modified_at=None,
            scanned_at=datetime.now(UTC),
        )
        self.db.commit()

    def plans(self) -> ExecutionPlanService:
        enqueued: list[uuid.UUID] = []
        return ExecutionPlanService(self.db, enqueue_execution_job=enqueued.append)


@pytest.fixture
def disk(db: Session) -> _Disk:
    return _Disk(db)


def _proposals(turn) -> list[dict]:  # noqa: ANN001
    return [b for b in turn.assistant_message.blocks if b["type"] == "action_proposal"]


def _proposed_ids(turn) -> set[str]:  # noqa: ANN001
    return {item["id"] for proposal in _proposals(turn) for item in proposal["items"]}


# -- broad requests never become deletion ----------------------------------------------


@requires_infra
def test_a_broad_cleanup_request_proposes_no_removal(disk: _Disk) -> None:
    disk.file("Holiday.mp4")
    disk.at("/old/notes.txt")

    for request in ("Clean up my storage", "Free up space on my computer"):
        assert _proposals(disk.ask(request)) == []


@requires_infra
def test_asking_to_permanently_delete_everything_proposes_nothing(disk: _Disk) -> None:
    disk.file("Holiday.mp4")

    turn = disk.ask("Permanently delete everything")

    assert _proposals(turn) == []
    assert ExecutionPlanRepository(disk.db).list_for_organization(disk.org.id) == []


@requires_infra
def test_files_that_only_resemble_a_request_are_never_proposed_for_removal(disk: _Disk) -> None:
    disk.at("/scratch/draft.txt", size=10)
    disk.at("/misc/data.bin", size=10)

    turn = disk.ask("Delete the unnecessary files")

    assert _proposals(turn) == []
    assert "only look similar" in turn.assistant_message.content


@requires_infra
def test_a_model_cannot_turn_a_request_into_a_removal(disk: _Disk) -> None:
    script = disk.at("/tools/cleanup.py")
    model = _ScriptedModel("m", answer=f'{{"kind": "trash", "query": "{script.name}"}}')

    turn = disk.ask("Get my workspace lean", model=model)

    assert _proposals(turn) == []


# -- source code and projects ---------------------------------------------------------------


@requires_infra
def test_a_python_file_in_a_project_is_never_proposed_for_removal(disk: _Disk) -> None:
    disk.at("/AI_Vault/pyproject.toml")
    main = disk.at("/AI_Vault/app/main.py")

    turn = disk.ask("Delete main.py")

    assert str(main.id) not in _proposed_ids(turn)
    assert "protected" in turn.assistant_message.content


@requires_infra
def test_a_python_file_the_model_calls_unnecessary_cannot_be_trashed(disk: _Disk) -> None:
    disk.at("/AI_Vault/pyproject.toml")
    helper = disk.at("/AI_Vault/app/legacy_helpers.py")

    with pytest.raises(ValidationError, match="protected"):
        disk.plans().create_ad_hoc_plan(
            [helper.id],
            action_type=ExecutionActionType.ARCHIVE,
            organization_id=disk.org.id,
            user_id=disk.user.id,
            require_approval=False,
        )
    assert FileRepository(disk.db).get_by_id(helper.id).trashed is False


@requires_infra
def test_git_and_manifests_mark_every_file_of_the_project_as_protected(disk: _Disk) -> None:
    disk.git_folder("/site/.git")
    logo = disk.at("/site/public/logo.png")
    readme_elsewhere = disk.at("/Pictures/beach.png")

    protection = AssetProtection(disk.db)

    assert any("software project in /site" in r for r in protection.reasons(logo))
    assert protection.reasons(readme_elsewhere) == []


# -- duplicates ----------------------------------------------------------------------------------


@requires_infra
def test_empty_files_are_never_grouped_as_duplicates(disk: _Disk) -> None:
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    for package in ("backend/app", "worker/worker", "shared/vault_shared/db"):
        disk.at(f"/AI_Vault/{package}/__init__.py", size=0, checksum=empty)
    disk.analyze()

    groups, _total = DuplicateGroupRepository(disk.db).list_for_organization(
        disk.org.id, limit=100, offset=0
    )
    assert [g for g in groups if g.checksum == empty] == []


@requires_infra
def test_duplicate_python_files_are_not_removed(disk: _Disk) -> None:
    disk.at("/project-a/utils.py", checksum="same-util")
    disk.at("/project-b/utils.py", checksum="same-util")
    disk.analyze()

    turn = disk.ask("Delete the duplicates")

    assert _proposals(turn) == []
    assert "protected" in turn.assistant_message.content
    groups, _total = DuplicateGroupRepository(disk.db).list_for_organization(
        disk.org.id, limit=100, offset=0
    )
    group = groups[0]
    with pytest.raises(ValidationError, match="protected"):
        disk.plans().create_plan_from_duplicate_group(
            group.id, organization_id=disk.org.id, user_id=disk.user.id, require_approval=False
        )


@requires_infra
def test_a_duplicate_cleanup_leaves_protected_copies_out_and_says_so(disk: _Disk) -> None:
    disk.at("/Photos/beach.jpg", checksum="beach")
    copy = disk.at("/Downloads/beach.jpg", checksum="beach")
    disk.at("/web/package.json")
    disk.at("/web/public/hero.jpg", checksum="hero")
    disk.at("/Photos/hero.jpg", checksum="hero")
    disk.analyze()

    proposal = _block(disk.ask("Delete the duplicates"), "action_proposal")

    assert [item["id"] for item in proposal["items"]] == [str(copy.id)]
    assert proposal["protected_count"] == 1
    assert proposal["protected_items"][0]["path"] == "/web/public/hero.jpg"


# -- weak evidence leaves files alone -----------------------------------------------------------


@requires_infra
def test_an_unclassified_file_is_left_untouched(disk: _Disk) -> None:
    unknown = disk.at("/inbox/scan_7f3a.bin")

    turn = disk.ask("Remove the files you can't classify")

    assert str(unknown.id) not in _proposed_ids(turn)
    assert FileRepository(disk.db).get_by_id(unknown.id).trashed is False


@requires_infra
def test_age_and_size_alone_are_not_actionable_recommendations(disk: _Disk) -> None:
    from vault_shared.execution.plan_service import is_executable_rule

    assert not is_executable_rule("archive_candidates")
    assert not is_executable_rule("large_unused_files")


# -- what the user explicitly asks for still works -----------------------------------------


@requires_infra
def test_an_explicit_trash_of_ordinary_files_is_planned(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")

    plan = disk.plans().create_ad_hoc_plan(
        [photo.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=disk.org.id,
        user_id=disk.user.id,
        require_approval=False,
    )

    steps = ExecutionStepRepository(disk.db).list_for_plan(plan.id)
    assert [s.target_file_id for s in steps] == [photo.id]


@requires_infra
def test_a_protected_file_the_user_picked_and_acknowledged_can_be_moved(disk: _Disk) -> None:
    disk.at("/AI_Vault/pyproject.toml")
    script = disk.at("/AI_Vault/scripts/seed.py")

    plan = disk.plans().create_ad_hoc_plan(
        [script.id],
        action_type=ExecutionActionType.MOVE_FILE,
        organization_id=disk.org.id,
        user_id=disk.user.id,
        new_parent_id="destination",
        require_approval=False,
        allow_protected=True,
    )

    step = ExecutionStepRepository(disk.db).list_for_plan(plan.id)[0]
    override = step.planned_change["protection_override"]
    assert override["confirmed_by_user_id"] == str(disk.user.id)
    audit = ExecutionAuditRepository(disk.db).list_for_plan(plan.id)[0]
    assert audit.metadata_["protected_included_count"] == 1


@requires_infra
def test_permanent_deletion_needs_its_own_typed_confirmation(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")
    plans = disk.plans()
    for wrong in (None, "DELETE", "yes", permanent_delete_confirmation(2)):
        with pytest.raises(ValidationError, match="confirmation"):
            plans.create_permanent_delete_plan(
                [photo.id],
                organization_id=disk.org.id,
                user_id=disk.user.id,
                confirmation=wrong,
                require_approval=False,
            )

    # The right phrase gets past the confirmation to the other gates (the
    # file must already be in Trash and backed up by an archive).
    with pytest.raises(ValidationError, match="eligible"):
        plans.create_permanent_delete_plan(
            [photo.id],
            organization_id=disk.org.id,
            user_id=disk.user.id,
            confirmation=permanent_delete_confirmation(1),
            require_approval=False,
        )


# -- truthful outcomes ---------------------------------------------------------------------------


@requires_infra
def test_a_disconnected_storage_cannot_start_a_removal(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")
    disk.computer.status = "reauth_required"
    disk.db.commit()

    with pytest.raises(ValidationError, match="not connected"):
        disk.plans().create_ad_hoc_plan(
            [photo.id],
            action_type=ExecutionActionType.ARCHIVE,
            organization_id=disk.org.id,
            user_id=disk.user.id,
            require_approval=False,
        )


def _finish(disk: _Disk, plan, *, step_status: str, result: str, verification: str) -> None:  # noqa: ANN001
    step = ExecutionStepRepository(disk.db).list_for_plan(plan.id)[0]
    step.status = step_status
    job = ExecutionJobRepository(disk.db).list_for_plan(plan.id)[0]
    ExecutionResultRepository(disk.db).create(
        execution_job_id=job.id,
        execution_step_id=step.id,
        status=result,
        verification_status=verification,
        error="Drive refused the change." if result == ExecutionResultStatus.FAILED else None,
        executed_at=datetime.now(UTC),
        verified_at=None,
    )
    plan.status = "completed" if result == ExecutionResultStatus.SUCCESS else "failed"
    disk.db.commit()


@requires_infra
def test_a_failed_removal_is_never_presented_as_done(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")
    plan = disk.plans().create_ad_hoc_plan(
        [photo.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=disk.org.id,
        user_id=disk.user.id,
        require_approval=False,
    )
    _finish(
        disk,
        plan,
        step_status=ExecutionStepStatus.FAILED,
        result=ExecutionResultStatus.FAILED,
        verification=VerificationStatus.SKIPPED,
    )

    action = ActionService(disk.db, job_service=None).get(plan.id, organization_id=disk.org.id)  # type: ignore[arg-type]

    assert action.status == "failed"
    assert action.message.startswith("Nothing was moved to Trash")
    assert action.problems[0].file_name == "blurry.jpg"


@requires_infra
def test_a_change_the_storage_did_not_confirm_is_not_reported_as_confirmed(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")
    plan = disk.plans().create_ad_hoc_plan(
        [photo.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=disk.org.id,
        user_id=disk.user.id,
        require_approval=False,
    )
    _finish(
        disk,
        plan,
        step_status=ExecutionStepStatus.COMPLETED,
        result=ExecutionResultStatus.SUCCESS,
        verification=VerificationStatus.FAILED,
    )

    action = ActionService(disk.db, job_service=None).get(plan.id, organization_id=disk.org.id)  # type: ignore[arg-type]

    assert action.status == "partial"
    assert "could not be confirmed" in action.message
    assert "— confirmed" not in action.message


@requires_infra
def test_removing_a_folder_from_the_index_keeps_what_was_done_to_its_files(disk: _Disk) -> None:
    photo = disk.at("/Photos/blurry.jpg")
    provider_file_id = photo.provider_file_id
    plan = disk.plans().create_ad_hoc_plan(
        [photo.id],
        action_type=ExecutionActionType.ARCHIVE,
        organization_id=disk.org.id,
        user_id=disk.user.id,
        require_approval=False,
    )

    disk.db.delete(disk.source)  # what removing a folder from the agent does
    disk.db.commit()
    disk.db.expire_all()

    step = ExecutionStepRepository(disk.db).list_for_plan(plan.id)[0]
    assert step.target_file_id is None
    assert step.pre_state["path"] == "/Photos/blurry.jpg"
    assert step.pre_state["provider_file_id"] == provider_file_id
