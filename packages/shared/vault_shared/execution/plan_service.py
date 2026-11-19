import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared import ConflictError, NotFoundError, ValidationError, get_settings
from vault_shared.db.models import (
    ExecutionActionType,
    ExecutionPlan,
    ExecutionPlanStatus,
    ExecutionStep,
    File,
    Folder,
    MemoryType,
    RecommendationRiskLevel,
    RecommendationStatus,
)
from vault_shared.db.repositories import (
    ApprovalRequestRepository,
    ArchiveJobRepository,
    AuditLogRepository,
    ConnectorCredentialsRepository,
    DuplicateGroupRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionStepRepository,
    FileRepository,
    FolderRepository,
    OrganizationMemoryRepository,
    OrganizationRecommendationRepository,
    RecommendationRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.execution.permission_validation import validate_execution_permissions
from vault_shared.formatting import human_bytes
from vault_shared.storage.models import Revision

# Only these Phase 7 rules map to a Phase 8-supported action type — every
# other rule (security/collaboration/productivity/knowledge findings) has
# no concrete, safe storage mutation behind it yet. `create_plan` rejects
# any other rule_name with a clear `ValidationError` rather than silently
# doing nothing.
_EXECUTABLE_RULES: dict[str, str] = {
    "duplicate_files": ExecutionActionType.REMOVE_DUPLICATE,
    "archive_candidates": ExecutionActionType.ARCHIVE,
    "large_unused_files": ExecutionActionType.ARCHIVE,
}


def is_executable_rule(rule_name: str) -> bool:
    return rule_name in _EXECUTABLE_RULES


# Storage Intelligence's ad-hoc plans (large/old/inactive/temporary-
# candidate listings, where the user picks specific files directly rather
# than acting on a precomputed group) are deliberately restricted to a
# narrow allowlist — the same philosophy as `_EXECUTABLE_RULES` (ADR-020).
# ARCHIVE (Drive Trash) is reversible; CREATE_ARCHIVE (Archive MVP) never
# mutates Drive at all, only reads. RENAME/MOVE_FILE are the Files browser's
# real file-explorer operations (each needs its own `new_name`/
# `new_parent_id` parameter, validated in `create_ad_hoc_plan`).
# UPDATE_METADATA/MOVE_FOLDER have no caller yet and stay unreachable.
_AD_HOC_ALLOWED_ACTIONS = frozenset(
    {
        ExecutionActionType.ARCHIVE,
        ExecutionActionType.CREATE_ARCHIVE,
        ExecutionActionType.RENAME,
        ExecutionActionType.MOVE_FILE,
        ExecutionActionType.RESTORE,
    }
)
# Actions whose targets are (or may be) in Trash — every other ad-hoc action
# only ever targets active files.
_TRASH_AWARE_ACTIONS = frozenset({ExecutionActionType.CREATE_ARCHIVE, ExecutionActionType.RESTORE})
_MAX_AD_HOC_FILES = 500

# Execution risk is a different question than the recommendation's own
# risk_level (which describes the *business* risk of the underlying
# storage/security issue) — every action this phase supports is fully
# reversible (Drive Trash, not deletion), so execution risk is really
# about blast radius: how many files does one approval affect at once.
_RISK_LOW_MAX_FILES = 10
_RISK_MEDIUM_MAX_FILES = 100


@dataclass(frozen=True)
class ExecutionPlanDetail:
    plan: ExecutionPlan
    steps: list[ExecutionStep]


class ExecutionPlanService:
    """The Execution Planner (Phase 8 spec) — converts one `Recommendation`
    into a deterministic `ExecutionPlan`. Reads only already-stored
    `File`/`Recommendation` state; makes no Drive call itself.

    ADR-026 (Phase 2): direct, user-triggered plans (every backend
    `POST /v1/execution-plans*` call) no longer wait on a human
    `ApprovalRequest` — every action already auto-approved itself
    instantly (Phase 0's instant-execution mode), so the approval step
    was pure overhead with no real review ever happening. `require_approval`
    (default `True`, preserving prior behavior) controls this per call:
    `False` marks the plan `APPROVED` and enqueues its `ExecutionJob`
    directly, in the same transaction, with no `ApprovalRequest` row ever
    created. The Automation Engine's `WorkflowPolicy` (Phase 9) is
    unaffected — Phase 9's `EXECUTE_ACTION` node calls `create_plan` with
    the default, so a policy's `require_approval`/`auto_execute`/`skip`
    effect keeps meaning exactly what it always has: a *human reviewer*
    deciding a *workflow's own* automated action, a distinct concept from
    "did anyone ever actually review this one-off rename."

    Lives in `packages/shared`, not `apps/backend`, since Phase 9's
    `EXECUTE_ACTION` workflow node (`apps/worker/worker/workflow/`) needs
    to build plans too, not just the backend's `POST /v1/execution-plans`
    endpoint — same "promote to packages/shared once a second app needs
    it" reasoning ADR-015 used for the DB layer ahead of Phase 4.
    `apps/backend/app/application/execution_plan_service.py` subclasses
    this to inject `enqueue_execution_job`, the one thing that differs
    between the backend and worker callers."""

    def __init__(
        self,
        db: Session,
        *,
        enqueue_execution_job: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        self._db = db
        self._recommendations = RecommendationRepository(db)
        self._duplicate_groups = DuplicateGroupRepository(db)
        self._plans = ExecutionPlanRepository(db)
        self._steps = ExecutionStepRepository(db)
        self._approvals = ApprovalRequestRepository(db)
        self._jobs = ExecutionJobRepository(db)
        self._connectors = StorageConnectorRepository(db)
        self._credentials = ConnectorCredentialsRepository(db)
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)
        self._sources = StorageSourceRepository(db)
        self._archive_jobs = ArchiveJobRepository(db)
        self._execution_audits = ExecutionAuditRepository(db)
        self._audit_logs = AuditLogRepository(db)
        self._organization_recommendations = OrganizationRecommendationRepository(db)
        self._organization_memories = OrganizationMemoryRepository(db)
        self._enqueue_execution_job = enqueue_execution_job

    def create_plan(
        self,
        recommendation_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        require_approval: bool = True,
    ) -> ExecutionPlan:
        recommendation = self._recommendations.get_owned(
            recommendation_id, organization_id=organization_id
        )
        if recommendation is None:
            raise NotFoundError("Recommendation not found.")
        if recommendation.status != RecommendationStatus.ACTIVE:
            raise ConflictError("Only active recommendations can be turned into an execution plan.")

        action_type = _EXECUTABLE_RULES.get(recommendation.rule_name)
        if action_type is None:
            raise ValidationError(
                f"Recommendations from rule '{recommendation.rule_name}' have no supported "
                "execution action yet — this category is view-only for now."
            )
        if self._plans.has_active_plan_for_recommendation(recommendation_id):
            raise ConflictError(
                "An execution plan is already pending or in progress for this recommendation."
            )

        file_ids = [uuid.UUID(fid) for fid in recommendation.affected_file_ids]
        files_by_id = {f.id: f for f in self._files.list_by_ids(file_ids)}
        ordered_files = [files_by_id[fid] for fid in file_ids if fid in files_by_id]
        if not ordered_files:
            raise ValidationError(
                "None of this recommendation's affected files could be found — it may be "
                "stale. Refresh recommendations and try again."
            )

        return self._finalize_plan(
            organization_id=organization_id,
            user_id=user_id,
            recommendation_id=recommendation_id,
            duplicate_group_id=None,
            action_type=action_type,
            ordered_files=ordered_files,
            audit_metadata={"recommendation_id": str(recommendation_id)},
            require_approval=require_approval,
        )

    def create_plan_from_duplicate_group(
        self,
        duplicate_group_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        require_approval: bool = True,
    ) -> ExecutionPlan:
        """Storage Intelligence's (post-hardening) equivalent of `create_plan`
        — a `DuplicateGroup` is already a concrete, content-verified set of
        exact-duplicate files (ADR-023's checksum `GROUP BY`, not a
        filename heuristic), so unlike a `Recommendation` there is no
        rule-name allowlist to check: every duplicate group is executable
        by construction. Only the non-`is_recommended_keep` members become
        `REMOVE_DUPLICATE` steps — the recommended copy is always left
        alone. See ADR-024."""
        group = self._duplicate_groups.get_owned(
            duplicate_group_id, organization_id=organization_id
        )
        if group is None:
            raise NotFoundError("Duplicate group not found.")
        if self._plans.has_active_plan_for_duplicate_group(duplicate_group_id):
            raise ConflictError(
                "An execution plan is already pending or in progress for this duplicate group."
            )

        members = self._duplicate_groups.list_members_with_files(duplicate_group_id)
        ordered_files = [file for member, file in members if not member.is_recommended_keep]
        if not ordered_files:
            raise ValidationError(
                "This duplicate group has no removable copies — it may be stale. "
                "Re-analyze storage and try again."
            )

        return self._finalize_plan(
            organization_id=organization_id,
            user_id=user_id,
            recommendation_id=None,
            duplicate_group_id=duplicate_group_id,
            action_type=ExecutionActionType.REMOVE_DUPLICATE,
            ordered_files=ordered_files,
            audit_metadata={"duplicate_group_id": str(duplicate_group_id)},
            require_approval=require_approval,
        )

    def create_ad_hoc_plan(
        self,
        file_ids: list[uuid.UUID],
        *,
        action_type: str,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        new_name: str | None = None,
        new_parent_id: str | None = None,
        remove_originals: bool = False,
        require_approval: bool = True,
    ) -> ExecutionPlan:
        """Storage Intelligence's large/old/inactive/temporary-candidate
        listings are live queries, not precomputed groups (ADR-023) — there
        is no `DuplicateGroup`-shaped row to originate a plan from, only the
        set of file ids the user picked on that listing page. Restricted to
        `_AD_HOC_ALLOWED_ACTIONS` and re-verifies every id actually belongs
        to this organization via `FileRepository.list_owned_by_organization`
        — unlike the other two `create_plan*` methods, nothing upstream
        already guaranteed that for a caller-supplied id list.

        `new_name`/`new_parent_id` are the Files browser's real
        file-explorer operations' one extra parameter each: a `RENAME` plan
        always targets exactly one file (renaming N files to the same
        literal string has no sensible meaning), while `MOVE_FILE` moves
        every selected file to the same destination folder."""
        if action_type not in _AD_HOC_ALLOWED_ACTIONS:
            raise ValidationError(f"'{action_type}' is not a supported ad-hoc action.")
        if not file_ids:
            raise ValidationError("Select at least one file.")
        if len(file_ids) > _MAX_AD_HOC_FILES:
            raise ValidationError(f"Select at most {_MAX_AD_HOC_FILES} files at a time.")
        if action_type == ExecutionActionType.RENAME:
            if len(file_ids) != 1:
                raise ValidationError("Rename one file at a time.")
            if not new_name or not new_name.strip():
                raise ValidationError("A new name is required.")
        if action_type == ExecutionActionType.MOVE_FILE and not new_parent_id:
            raise ValidationError("A destination folder is required.")

        if action_type in _TRASH_AWARE_ACTIONS:
            candidates = self._files.list_owned_by_organization_including_trashed(
                file_ids, organization_id=organization_id
            )
            ordered_files = [
                file
                for file in candidates
                if file.permanently_deleted_at is None
                and (file.trashed or action_type != ExecutionActionType.RESTORE)
            ]
        else:
            ordered_files = self._files.list_owned_by_organization(
                file_ids, organization_id=organization_id
            )
        if not ordered_files:
            raise ValidationError(
                "None of the selected files could be found — they may be stale. "
                "Refresh and try again."
            )

        planned_change_by_file_id: dict[uuid.UUID, dict] | None = None
        if action_type == ExecutionActionType.RENAME:
            planned_change_by_file_id = {
                ordered_files[0].id: {"action": action_type, "new_name": (new_name or "").strip()}
            }
        elif action_type == ExecutionActionType.CREATE_ARCHIVE:
            planned_change_by_file_id = {
                file.id: {"action": action_type, "remove_originals": remove_originals}
                for file in ordered_files
            }
        elif action_type == ExecutionActionType.MOVE_FILE:
            planned_change_by_file_id = {
                file.id: {"action": action_type, "new_parent_id": new_parent_id}
                for file in ordered_files
            }
            assert new_parent_id is not None  # validated above
            self._record_correction_if_diverges_from_recommendation(
                organization_id=organization_id,
                ordered_files=ordered_files,
                new_parent_id=new_parent_id,
            )

        return self._finalize_plan(
            organization_id=organization_id,
            user_id=user_id,
            recommendation_id=None,
            duplicate_group_id=None,
            action_type=action_type,
            ordered_files=ordered_files,
            audit_metadata={"ad_hoc_action": action_type, "requested_file_count": len(file_ids)},
            planned_change_by_file_id=planned_change_by_file_id,
            require_approval=require_approval,
        )

    def create_permanent_delete_plan(
        self,
        file_ids: list[uuid.UUID],
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        require_approval: bool = True,
    ) -> ExecutionPlan:
        """Real, unrecoverable Drive deletion (`files.delete`) — everything
        else this class builds is Drive-Trash-reversible. Deliberately kept
        out of `create_ad_hoc_plan`/`_AD_HOC_ALLOWED_ACTIONS` entirely
        (its own method, its own validation) rather than one more allowed
        action type there, so a caller can never reach PERMANENT_DELETE by
        accident through the generic path.

        Three things make this safe to expose at all:
        - Only files already `trashed` (and not yet
          `permanently_deleted_at`) are eligible — you can't permanently
          delete something that hasn't already been trashed first,
          mirroring how you'd have to empty Drive's own Trash by hand.
        - Only files already backed up by a `COMPLETED` archive
          (`ArchiveJobRepository.list_archived_file_ids`) are eligible —
          real deletion is never the only copy of a file's content
          anywhere; the founder must "Create Archive" it first. A
          later-deleted archive revokes this eligibility too (see that
          method's docstring).
        - `rollback_available=False` on the resulting plan. The caller
          (the execution-plans router) requires an explicit frontend
          confirmation dialog before ever calling this method — the one
          action kind this irreversible always gets a real "are you sure"
          step, per ADR-026, regardless of `require_approval`."""
        if not file_ids:
            raise ValidationError("Select at least one file.")
        if len(file_ids) > _MAX_AD_HOC_FILES:
            raise ValidationError(f"Select at most {_MAX_AD_HOC_FILES} files at a time.")

        candidates = self._files.list_owned_by_organization_including_trashed(
            file_ids, organization_id=organization_id
        )
        archived_file_ids = self._archive_jobs.list_archived_file_ids(organization_id)
        ordered_files = [
            file
            for file in candidates
            if file.trashed and file.permanently_deleted_at is None and file.id in archived_file_ids
        ]
        if not ordered_files:
            raise ValidationError(
                "None of the selected files are eligible — a file must already be in Trash "
                "and backed up by a completed archive (Create Archive) before it can be "
                "permanently deleted."
            )

        return self._finalize_plan(
            organization_id=organization_id,
            user_id=user_id,
            recommendation_id=None,
            duplicate_group_id=None,
            action_type=ExecutionActionType.PERMANENT_DELETE,
            ordered_files=ordered_files,
            audit_metadata={
                "ad_hoc_action": ExecutionActionType.PERMANENT_DELETE,
                "requested_file_count": len(file_ids),
            },
            rollback_available=False,
            require_approval=require_approval,
        )

    def _provider_of(self, files: list[File]) -> str:
        """The storage provider all of a plan's files live in. One action
        works on one storage, so a mix is refused."""
        providers: set[str] = set()
        for source_id in {file.storage_source_id for file in files}:
            source = self._sources.get_by_id(source_id)
            connector = self._connectors.get_by_id(source.connector_id) if source else None
            if connector is not None:
                providers.add(connector.provider)
        if len(providers) != 1:
            raise ValidationError("Choose files from one storage at a time.")
        return providers.pop()

    def _finalize_plan(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        recommendation_id: uuid.UUID | None,
        duplicate_group_id: uuid.UUID | None,
        action_type: str,
        ordered_files: list[File],
        audit_metadata: dict,
        require_approval: bool,
        planned_change_by_file_id: dict[uuid.UUID, dict] | None = None,
        rollback_available: bool = True,
    ) -> ExecutionPlan:
        total_bytes = sum(f.size_bytes or 0 for f in ordered_files)
        provider = self._provider_of(ordered_files)
        plan = self._plans.create(
            organization_id=organization_id,
            recommendation_id=recommendation_id,
            duplicate_group_id=duplicate_group_id,
            created_by_user_id=user_id,
            target_provider=provider,
            estimated_impact=f"{len(ordered_files)} files, ~{human_bytes(total_bytes)}",
            estimated_storage_savings_bytes=total_bytes or None,
            risk_level=self._risk_level_for(len(ordered_files)),
            rollback_available=rollback_available,
            required_permissions=[f"{provider}:write"],
        )
        for index, file in enumerate(ordered_files):
            planned_change = (planned_change_by_file_id or {}).get(file.id, {"action": action_type})
            self._steps.create(
                execution_plan_id=plan.id,
                step_order=index,
                action_type=action_type,
                target_file_id=file.id,
                pre_state=self._pre_state(file),
                planned_change=planned_change,
            )

        self._execution_audits.record(
            organization_id=organization_id,
            execution_plan_id=plan.id,
            actor_user_id=user_id,
            event_type="execution_plan_created",
            metadata={**audit_metadata, "step_count": len(ordered_files)},
        )
        self._audit_logs.record(
            event_type="execution_plan_created",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"execution_plan_id": str(plan.id)},
        )

        if require_approval:
            settings = get_settings()
            expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours)
            self._approvals.create(
                execution_plan_id=plan.id,
                organization_id=organization_id,
                requested_by_user_id=user_id,
                expires_at=expires_at,
            )
            self._db.commit()
            return plan

        # ADR-026: no `ApprovalRequest` for a direct, user-triggered plan —
        # go straight to APPROVED and start the job in the same transaction
        # a human `decide()` approval would have produced. Same permission
        # pre-check `ApprovalService.decide()`/`_check_permissions` always
        # ran before approving, so a missing write scope still surfaces as
        # an immediate, synchronous error here instead of only failing
        # later, silently, on the worker.
        if self._enqueue_execution_job is None:
            raise ValidationError(
                "This ExecutionPlanService instance cannot start a plan without approval — "
                "it was constructed without an enqueue_execution_job callback."
            )
        connector = self._connectors.get_by_organization_and_provider(
            organization_id=organization_id, provider=plan.target_provider
        )
        credentials = self._credentials.get_by_connector_id(connector.id) if connector else None
        failures = validate_execution_permissions(connector=connector, credentials=credentials)
        if failures:
            raise ValidationError(
                "Cannot start — execution permissions are not satisfied: " + "; ".join(failures)
            )
        self._plans.update_status(plan, status=ExecutionPlanStatus.APPROVED)
        job = self._jobs.create(
            execution_plan_id=plan.id, organization_id=organization_id, triggered_by_user_id=user_id
        )
        self._execution_audits.record(
            organization_id=organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            actor_user_id=user_id,
            event_type="execution_approved",
            message="No approval step required — this action runs immediately.",
        )
        self._audit_logs.record(
            event_type="execution_approved",
            organization_id=organization_id,
            user_id=user_id,
            metadata={"execution_plan_id": str(plan.id), "execution_job_id": str(job.id)},
        )
        self._db.commit()
        self._enqueue_execution_job(job.id)
        return plan

    def get_detail(
        self, execution_plan_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> ExecutionPlanDetail:
        plan = self._plans.get_owned(execution_plan_id, organization_id=organization_id)
        if plan is None:
            raise NotFoundError("Execution plan not found.")
        return ExecutionPlanDetail(plan=plan, steps=self._steps.list_for_plan(plan.id))

    def list_for_organization(
        self, organization_id: uuid.UUID, *, status: str | None = None
    ) -> list[ExecutionPlan]:
        return self._plans.list_for_organization(organization_id, status=status)

    def _record_correction_if_diverges_from_recommendation(
        self,
        *,
        organization_id: uuid.UUID,
        ordered_files: list[File],
        new_parent_id: str,
    ) -> None:
        """Phase 2 organizational memory (spec: "structured user
        corrections... should influence future recommendations"). A user
        moving a file via the Files browser to somewhere other than what
        an active `OrganizationRecommendation` suggested for it is a real,
        observed fact worth remembering — recorded here, the one place a
        MOVE_FILE ad-hoc plan is actually created, rather than guessed at
        from execution results later. Pure DB reads/writes, no AI call:
        `packages/shared/vault_shared/execution/` must stay clear of any
        `vault_shared.ai_gateway` import (`test_execution_code_never_
        depends_on_the_ai_gateway`), and this hook doesn't need one."""
        recommendations = self._organization_recommendations.list_active_for_files(
            organization_id, [file.id for file in ordered_files]
        )
        for recommendation in recommendations:
            if recommendation.entity_id is None or not recommendation.suggested_destination:
                continue
            suggested_folder = self._resolve_existing_folder(
                storage_source_id=ordered_files[0].storage_source_id,
                path_parts=recommendation.suggested_destination,
            )
            if suggested_folder is not None and suggested_folder.provider_file_id == new_parent_id:
                continue  # the user moved it exactly where this recommendation suggested
            self._organization_memories.create(
                organization_id=organization_id,
                memory_type=MemoryType.CORRECTION,
                key=f"entity:{recommendation.entity_id}",
                value={
                    "organization_recommendation_id": str(recommendation.id),
                    "suggested_destination": recommendation.suggested_destination,
                    "actual_new_parent_id": new_parent_id,
                },
                evidence=(
                    "A user moved a file to a different location than this active "
                    "organization recommendation suggested."
                ),
                confidence=1.0,
            )

    def _resolve_existing_folder(
        self, *, storage_source_id: uuid.UUID, path_parts: list[str]
    ) -> Folder | None:
        parent_folder_id: uuid.UUID | None = None
        folder: Folder | None = None
        for part in path_parts:
            folder = self._folders.get_by_parent_and_name(
                storage_source_id=storage_source_id, parent_folder_id=parent_folder_id, name=part
            )
            if folder is None:
                return None
            parent_folder_id = folder.id
        return folder

    @staticmethod
    def _risk_level_for(file_count: int) -> str:
        if file_count <= _RISK_LOW_MAX_FILES:
            return RecommendationRiskLevel.LOW
        if file_count <= _RISK_MEDIUM_MAX_FILES:
            return RecommendationRiskLevel.MEDIUM
        return RecommendationRiskLevel.HIGH

    @staticmethod
    def _pre_state(file: File) -> dict:
        """What the reviewer saw. `revision` is the file's version as of the
        last scan; the Execution Engine refuses to apply the step if the
        provider now reports a different content revision (the file was
        edited after the plan was made)."""
        return {
            "parent_folder_id": str(file.parent_folder_id) if file.parent_folder_id else None,
            "name": file.name,
            "revision": Revision(
                token=file.version_id, modified_at=file.provider_modified_at
            ).to_dict(),
        }
