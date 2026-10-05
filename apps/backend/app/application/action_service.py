import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.application.execution_job_service import ExecutionJobService
from vault_shared import NotFoundError
from vault_shared.db.models import (
    ExecutionActionType,
    ExecutionJobStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
    ExecutionResultStatus,
    ExecutionStepStatus,
    VerificationStatus,
    provider_display_name,
)
from vault_shared.db.repositories import (
    ArchiveJobRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
)

# How a finished action reads to a user — the noun phrase for the file
# count, then what happened to them.
_VERBS: dict[str, str] = {
    ExecutionActionType.RENAME: "renamed",
    ExecutionActionType.MOVE_FILE: "moved",
    ExecutionActionType.MOVE_FOLDER: "moved",
    ExecutionActionType.ARCHIVE: "moved to Trash",
    ExecutionActionType.REMOVE_DUPLICATE: "removed as duplicates (moved to Trash)",
    ExecutionActionType.RESTORE: "restored from Trash",
    ExecutionActionType.PERMANENT_DELETE: "permanently deleted",
    ExecutionActionType.CREATE_ARCHIVE: "archived",
    ExecutionActionType.UPDATE_METADATA: "updated",
}
_PROGRESS: dict[str, str] = {
    ExecutionActionType.RENAME: "Renaming {files} in {provider}…",
    ExecutionActionType.MOVE_FILE: "Moving {files} in {provider}…",
    ExecutionActionType.MOVE_FOLDER: "Moving {files} in {provider}…",
    ExecutionActionType.ARCHIVE: "Moving {files} to Trash in {provider}…",
    ExecutionActionType.REMOVE_DUPLICATE: "Moving {duplicates} to Trash in {provider}…",
    ExecutionActionType.RESTORE: "Restoring {files} from Trash in {provider}…",
    ExecutionActionType.PERMANENT_DELETE: "Permanently deleting {files} in {provider}…",
    ExecutionActionType.CREATE_ARCHIVE: "Archiving {files} to {provider}…",
}
_IN_PROGRESS = {
    ExecutionPlanStatus.PENDING_APPROVAL,
    ExecutionPlanStatus.APPROVED,
    ExecutionPlanStatus.EXECUTING,
}
_STATUS: dict[str, str] = {
    ExecutionPlanStatus.COMPLETED: "done",
    ExecutionPlanStatus.PARTIALLY_COMPLETED: "partial",
    ExecutionPlanStatus.FAILED: "failed",
    ExecutionPlanStatus.ROLLED_BACK: "undone",
    ExecutionPlanStatus.REJECTED: "cancelled",
    ExecutionPlanStatus.EXPIRED: "cancelled",
    ExecutionPlanStatus.CHANGES_REQUESTED: "cancelled",
}
_ACTIVITY_LIMIT = 20


@dataclass(frozen=True)
class ActionProblem:
    file_name: str
    reason: str


@dataclass(frozen=True)
class ActionArchive:
    destination_path: str | None
    destination_web_view_link: str | None
    original_size_bytes: int | None
    compressed_size_bytes: int | None
    originals_removed_count: int
    verified: bool


@dataclass(frozen=True)
class ActionResult:
    id: uuid.UUID
    kind: str
    status: str
    message: str
    total: int
    succeeded: int
    failed: int
    verified: int
    provider: str
    created_at: datetime
    can_undo: bool
    problems: list[ActionProblem] = field(default_factory=list)
    archive: ActionArchive | None = None


@dataclass(frozen=True)
class ActivityItem:
    id: str
    kind: str
    status: str
    message: str
    at: datetime


class ActionService:
    """The product's single view of "something the user asked AI Vault to
    do": what it was, whether it is still running, what the provider
    confirmed, and what went wrong — in plain words. Execution plans and jobs
    remain the internal machinery (verification, rollback, retries) and are
    never named to the user."""

    def __init__(self, db: Session, *, job_service: ExecutionJobService) -> None:
        self._plans = ExecutionPlanRepository(db)
        self._steps = ExecutionStepRepository(db)
        self._jobs = ExecutionJobRepository(db)
        self._results = ExecutionResultRepository(db)
        self._archives = ArchiveJobRepository(db)
        self._audits = ExecutionAuditRepository(db)
        self._files = FileRepository(db)
        self._job_service = job_service

    def get(self, action_id: uuid.UUID, *, organization_id: uuid.UUID) -> ActionResult:
        plan = self._plans.get_owned(action_id, organization_id=organization_id)
        if plan is None:
            raise NotFoundError("Action not found.")
        return self._describe(plan)

    def undo(
        self, action_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> ActionResult:
        self._job_service.trigger_rollback(
            action_id, organization_id=organization_id, user_id=user_id
        )
        return self.get(action_id, organization_id=organization_id)

    def recent_activity(self, organization_id: uuid.UUID) -> list[ActivityItem]:
        items = [
            ActivityItem(
                id=str(result.id),
                kind=result.kind,
                status=result.status,
                message=result.message,
                at=result.created_at,
            )
            for result in (
                self._describe(plan)
                for plan in self._plans.list_for_organization(
                    organization_id, limit=_ACTIVITY_LIMIT
                )
            )
            # An action whose files were all later deleted from storage has
            # nothing left to describe (their steps go with the file rows).
            if result.status != "cancelled" and result.total > 0
        ]
        for audit in self._audits.list_events_for_organization(
            organization_id, event_types=["folder_created", "file_created"], limit=_ACTIVITY_LIMIT
        ):
            noun = "Folder" if audit.event_type == "folder_created" else "File"
            items.append(
                ActivityItem(
                    id=str(audit.id),
                    kind=audit.event_type,
                    status="done",
                    message=f"{noun} “{audit.metadata_.get('name', '')}” created",
                    at=audit.created_at,
                )
            )
        items.sort(key=lambda item: item.at, reverse=True)
        return items[:_ACTIVITY_LIMIT]

    def _describe(self, plan: ExecutionPlan) -> ActionResult:
        steps = self._steps.list_for_plan(plan.id)
        kind = steps[0].action_type if steps else "unknown"
        total = len(steps)
        succeeded = sum(
            1
            for step in steps
            if step.status in (ExecutionStepStatus.COMPLETED, ExecutionStepStatus.ROLLED_BACK)
        )
        failed = sum(1 for step in steps if step.status == ExecutionStepStatus.FAILED)

        jobs = self._jobs.list_for_plan(plan.id)
        undoing = any(
            job.is_rollback
            and job.status in (ExecutionJobStatus.PENDING, ExecutionJobStatus.RUNNING)
            for job in jobs
        )
        forward_results = [
            result
            for job in jobs
            if not job.is_rollback
            for result in self._results.list_for_job(job.id)
        ]
        verified = sum(
            1
            for result in forward_results
            if result.status == ExecutionResultStatus.SUCCESS
            and result.verification_status == VerificationStatus.VERIFIED
        )
        failed_step_ids = {
            result.execution_step_id: result.error
            for result in forward_results
            if result.status == ExecutionResultStatus.FAILED
        }
        names = {
            file.id: file.name
            for file in self._files.list_by_ids([step.target_file_id for step in steps])
        }
        problems = [
            ActionProblem(
                file_name=names.get(step.target_file_id, "A file"),
                reason=failed_step_ids.get(step.id) or "It could not be completed.",
            )
            for step in steps
            if step.status == ExecutionStepStatus.FAILED
        ]

        status = "in_progress" if plan.status in _IN_PROGRESS else _STATUS.get(plan.status, "done")
        provider = provider_display_name(plan.target_provider)
        verb = _VERBS.get(kind, "processed")
        files = "file" if total == 1 else "files"
        if undoing:
            status = "in_progress"
            message = f"Undoing — putting {total} {files} back in {provider}…"
        elif status == "in_progress":
            message = _PROGRESS.get(kind, "Working on {files} in {provider}…").format(
                files=f"{total} {files}",
                duplicates=f"{total} duplicate {files}",
                provider=provider,
            )
        elif status == "undone":
            message = f"Undone — {succeeded} {files} put back the way they were"
        elif succeeded == 0:
            message = f"Nothing was {verb} — {failed} of {total} {files} failed"
        else:
            message = f"{succeeded} of {total} {files} {verb} in {provider}"
            if verified == succeeded:
                message += " — confirmed"
            if kind in (ExecutionActionType.ARCHIVE, ExecutionActionType.REMOVE_DUPLICATE):
                message += ". Google counts the space until Drive's Trash is emptied"

        archive = None
        if kind == ExecutionActionType.CREATE_ARCHIVE:
            archive_job = self._archives.get_by_plan_id(plan.id)
            if archive_job is not None:
                archive = ActionArchive(
                    destination_path=archive_job.destination_path,
                    destination_web_view_link=archive_job.destination_web_view_link,
                    original_size_bytes=archive_job.original_size_bytes,
                    compressed_size_bytes=archive_job.compressed_size_bytes,
                    originals_removed_count=archive_job.originals_removed_count,
                    verified=archive_job.verified_at is not None,
                )
                if status in ("done", "partial") and archive_job.destination_path:
                    folder = archive_job.destination_path.rsplit("/", 1)[0].lstrip("/")
                    kept = archive_job.originals_removed_count == 0
                    message = f"{succeeded} {files} zipped into '{folder}' in {provider} — " + (
                        "originals kept where they were"
                        if kept
                        else f"{archive_job.originals_removed_count} originals moved to Trash"
                    )

        return ActionResult(
            id=plan.id,
            kind=kind,
            status=status,
            message=message,
            total=total,
            succeeded=succeeded,
            failed=failed,
            verified=verified,
            provider=provider,
            created_at=plan.created_at,
            can_undo=(
                plan.rollback_available
                and status in ("done", "partial")
                and kind != ExecutionActionType.PERMANENT_DELETE
            ),
            problems=problems,
            archive=archive,
        )
