import enum
import hashlib
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from vault_shared import ConflictError, NotFoundError, ValidationError, get_logger, get_settings
from vault_shared.db.models import (
    ArchiveJobStatus,
    ConnectorStatus,
    ExecutionActionType,
    ExecutionJob,
    ExecutionJobStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
    ExecutionResultStatus,
    ExecutionStep,
    ExecutionStepStatus,
    File,
    RecommendationTrigger,
    RollbackRecord,
    StorageAnalysisTrigger,
    StorageConnector,
    VerificationStatus,
)
from vault_shared.db.repositories import (
    ArchiveJobRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    ExecutionResultRepository,
    ExecutionStepRepository,
    FileRepository,
    RecommendationJobRepository,
    RollbackRecordRepository,
    StorageAnalysisJobRepository,
    StorageConnectorRepository,
)
from vault_shared.object_storage import ObjectStorageClient
from vault_shared.storage import (
    ExportPurpose,
    ProviderFileId,
    Revision,
    StorageAdapter,
    StorageAdapterProvider,
    StorageFile,
    StorageStaleRevisionError,
    StorageUnsupportedError,
    read_bounded,
)
from worker.tasks.recommendation import run_recommendation
from worker.tasks.storage_intelligence import run_storage_intelligence

logger = get_logger("worker.execution.execution_service")

_TRASHABLE_ACTIONS = (ExecutionActionType.ARCHIVE, ExecutionActionType.REMOVE_DUPLICATE)
_MOVE_ACTIONS = (ExecutionActionType.MOVE_FILE, ExecutionActionType.MOVE_FOLDER)
# PENDING covers a fresh or resumed job; RUNNING is a job whose worker died
# mid-run (Celery redelivers under `acks_late`) — the plan lock, not the
# status, is what tells a crashed run from a concurrent one.
_RUNNABLE_JOB_STATUSES = (ExecutionJobStatus.PENDING, ExecutionJobStatus.RUNNING)


def plan_lock_key(plan_id: uuid.UUID) -> int:
    """The bigint Postgres advisory-lock key serializing one plan's runs."""
    return int.from_bytes(plan_id.bytes[:8], "big", signed=True)


class _StepProgress(enum.StrEnum):
    """Where a step's target stands relative to the pre-state captured
    before the first attempt and the state the step would produce."""

    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    CONFLICT = "conflict"


class ExecutionCancelled(Exception):
    """Unwinds `run()` once cooperative cancellation has been observed —
    mirrors `ScanCancelled`/`EnrichmentCancelled`/`EmbeddingCancelled`."""


class ExecutionPaused(Exception):
    """Unwinds `run()` once cooperative pause has been observed. Unlike
    cancellation, the job is left in a resumable state — `run()`'s
    top-level handler for this is a no-op, since the pausing branch
    already committed the `PAUSED` status before raising."""


class ExecutionService:
    """The Execution Engine (Handbook §8.7) — the *only* module in this
    codebase permitted to perform a mutating call against connected
    storage — the only caller of the `StorageAdapter` methods listed in
    `MUTATING_METHODS`. It reaches storage only through the adapter for the
    plan's connection (`storage.adapter_for(connector)`), so it is
    provider-independent; every organization, retry, conflict and lock
    guarantee below applies to any provider. `run()` branches on
    `ExecutionJob.is_rollback` to either
    carry out an approved plan's steps forward or reverse a plan's
    already-executed steps via their `RollbackRecord`s — both paths share
    one job lifecycle, cooperative cancel/pause, per-step failure
    isolation, and audit trail. See ADR-020."""

    def __init__(
        self,
        db: Session,
        *,
        storage: StorageAdapterProvider,
        object_storage_client: ObjectStorageClient,
    ) -> None:
        self._db = db
        self._storage = storage
        self._object_storage = object_storage_client
        self._connectors = StorageConnectorRepository(db)
        self._plans = ExecutionPlanRepository(db)
        self._steps = ExecutionStepRepository(db)
        self._jobs = ExecutionJobRepository(db)
        self._results = ExecutionResultRepository(db)
        self._rollback_records = RollbackRecordRepository(db)
        self._files = FileRepository(db)
        self._audits = ExecutionAuditRepository(db)
        self._archive_jobs = ArchiveJobRepository(db)
        self._storage_analysis_jobs = StorageAnalysisJobRepository(db)
        self._recommendation_jobs = RecommendationJobRepository(db)

    def run(self, execution_job_id: uuid.UUID) -> None:
        job = self._jobs.get_by_id(execution_job_id)
        if job is None:
            logger.warning(
                "execution_job_not_found", extra={"execution_job_id": str(execution_job_id)}
            )
            return

        if job.status not in _RUNNABLE_JOB_STATUSES:
            # A redelivery of a message whose job already finished (the ack
            # was lost), or a stale message for a job that has since been
            # paused or cancelled: never re-run it.
            logger.info(
                "execution_job_not_runnable",
                extra={"execution_job_id": str(job.id), "status": job.status},
            )
            return

        plan = self._plans.get_by_id(job.execution_plan_id)
        if plan is None:
            self._jobs.mark_failed(job, error="Execution plan no longer exists.")
            self._db.commit()
            return

        with self._exclusive_plan_lock(plan.id) as acquired:
            if not acquired:
                logger.warning(
                    "execution_plan_locked_by_another_worker",
                    extra={"execution_job_id": str(job.id), "execution_plan_id": str(plan.id)},
                )
                return
            # The job may have finished between the status check above and
            # taking the lock (duplicate delivery racing the first worker).
            self._db.refresh(job)
            if job.status not in _RUNNABLE_JOB_STATUSES:
                return
            self._run_job(job, plan)

    @contextmanager
    def _exclusive_plan_lock(self, plan_id: uuid.UUID) -> Iterator[bool]:
        """At most one worker executes a plan's steps at a time. A Postgres
        session-level advisory lock on a dedicated connection — not the ORM
        session's, whose connection is released at every commit — so it
        survives the engine's many commits and is released by the database
        itself if the worker process dies, which is what lets a redelivered
        crash-recovery run acquire it while a genuinely concurrent duplicate
        cannot."""
        key = plan_lock_key(plan_id)
        bind = self._db.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        connection = engine.connect()
        acquired = False
        try:
            acquired = bool(
                connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            )
            yield acquired
        finally:
            try:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            finally:
                connection.close()

    def _run_job(self, job: ExecutionJob, plan: ExecutionPlan) -> None:
        connector = self._connectors.get_by_organization_and_provider(
            organization_id=plan.organization_id, provider=plan.target_provider
        )
        failures = self._permission_failures(connector)
        if failures:
            error = "; ".join(failures)
            self._jobs.mark_failed(job, error=error)
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_failed",
                message=error,
            )
            self._db.commit()
            return

        self._jobs.mark_running(job)
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_started",
            metadata={"is_rollback": job.is_rollback},
        )
        if not job.is_rollback:
            self._plans.update_status(plan, status=ExecutionPlanStatus.EXECUTING)
        self._db.commit()

        assert connector is not None  # _permission_failures already checked this
        adapter = self._storage.adapter_for(connector)
        adapter.connect()

        try:
            if job.is_rollback:
                self._run_rollback(job, plan, adapter=adapter)
            else:
                self._run_forward(job, plan, adapter=adapter)
        except ExecutionCancelled:
            self._jobs.mark_cancelled(job)
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_cancelled",
            )
            self._db.commit()
        except ExecutionPaused:
            pass  # the pausing branch already committed PAUSED before raising
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception("execution_job_failed", extra={"execution_job_id": str(job.id)})
            self._db.rollback()
            self._jobs.mark_failed(job, error=str(exc))
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="execution_failed",
                message=str(exc),
            )
            self._db.commit()

    def _permission_failures(self, connector: StorageConnector | None) -> list[str]:
        """Defense-in-depth re-check right before executing, since time
        passes between approval and execution. Connector availability is
        application state; whether the *grant* allows mutations is a question
        for the provider's adapter (what "write access" means differs per
        provider), and no credential is read here."""
        if connector is None:
            return ["Connector no longer exists."]
        failures: list[str] = []
        if connector.status != ConnectorStatus.CONNECTED:
            failures.append(f"Connector is not connected (status: {connector.status}).")
        try:
            failures.extend(self._storage.adapter_for(connector).write_access_problems())
        except StorageUnsupportedError as exc:
            failures.append(exc.message)
        return failures

    # ------------------------------------------------------------------
    # Forward execution
    # ------------------------------------------------------------------

    def _run_forward(
        self, job: ExecutionJob, plan: ExecutionPlan, *, adapter: StorageAdapter
    ) -> None:
        steps = self._steps.list_for_plan(plan.id)

        # CREATE_ARCHIVE is the one action type that isn't "one step = one
        # independent mutation" — N files become ONE zip in ONE object
        # storage write, so all of a plan's CREATE_ARCHIVE steps are
        # completed together here, before the per-step loop below (which
        # only ever processes still-PENDING steps) ever reaches them.
        # Cancellation is checked once, before the batch starts, not
        # per-file within it — zip-building isn't resumable mid-stream.
        pending_archive_steps = [
            step
            for step in steps
            if step.action_type == ExecutionActionType.CREATE_ARCHIVE
            and step.status == ExecutionStepStatus.PENDING
        ]
        if pending_archive_steps:
            self._check_cancelled(job.id)
            self._execute_archive_batch(job, plan, pending_archive_steps, adapter=adapter)

        succeeded = 0
        failed = 0
        for step in steps:
            if step.status != ExecutionStepStatus.PENDING:
                # Resuming a previously-paused job — already-decided steps
                # are never re-executed.
                if step.status == ExecutionStepStatus.COMPLETED:
                    succeeded += 1
                elif step.status == ExecutionStepStatus.FAILED:
                    failed += 1
                continue

            self._check_cancelled(job.id)
            if self._jobs.is_pause_requested(job.id):
                self._jobs.mark_paused(job)
                self._audits.record(
                    organization_id=plan.organization_id,
                    execution_plan_id=plan.id,
                    execution_job_id=job.id,
                    event_type="execution_paused",
                )
                self._db.commit()
                raise ExecutionPaused

            if self._execute_forward_step(job, plan, step, adapter=adapter):
                succeeded += 1
            else:
                failed += 1

        if failed == 0:
            self._jobs.mark_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.COMPLETED)
        elif succeeded == 0:
            self._jobs.mark_failed(job, error="All steps failed.")
            self._plans.update_status(plan, status=ExecutionPlanStatus.FAILED)
        else:
            self._jobs.mark_partially_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.PARTIALLY_COMPLETED)

        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_completed",
            metadata={"succeeded": succeeded, "failed": failed},
        )
        self._db.commit()

        if any(
            step.action_type in _TRASHABLE_ACTIONS and step.status == ExecutionStepStatus.COMPLETED
            for step in steps
        ):
            self._trigger_storage_refresh(plan.organization_id)

    def _execute_forward_step(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        step: ExecutionStep,
        *,
        adapter: StorageAdapter,
    ) -> bool:
        now = datetime.now(UTC)
        already_applied = False
        try:
            file = self._get_plan_file(plan, step)

            # `ExecutionStep.id` is this step's idempotency identity, and
            # `rollback_records.execution_step_id` is unique. A record for a
            # still-PENDING step therefore means an earlier attempt died
            # (worker crash, `acks_late` redelivery) after capturing
            # pre-state and possibly after the Drive write. That record is
            # the authoritative pre-state and must survive the retry.
            record = self._rollback_records.get_by_step(step.id)

            # Live "resource existence" + "current file state" validation
            # (Phase 8 spec) — right before mutating, since provider state
            # can have drifted since the plan was created or even since
            # approval.
            current: StorageFile | None
            try:
                current = adapter.get_file(ProviderFileId(file.provider_file_id))
            except NotFoundError:
                # Only a retry of a permanent delete may find the file gone:
                # that is the mutation's own result.
                if record is None or step.action_type != ExecutionActionType.PERMANENT_DELETE:
                    raise
                current = None

            if record is None:
                assert current is not None  # NotFound is only swallowed when a record exists
                # PERMANENT_DELETE is the one action that *requires* the
                # file already be trashed (that's its whole eligibility rule,
                # see ExecutionPlanService.create_permanent_delete_plan);
                # every other action expects the opposite.
                if step.action_type == ExecutionActionType.PERMANENT_DELETE:
                    if not current.trashed:
                        raise ConflictError(
                            "File is no longer in Trash — refusing to delete it permanently."
                        )
                elif current.trashed:
                    raise ConflictError(
                        "File is already trashed outside this platform — nothing to do."
                    )
                self._reject_if_stale(step, current)
                # The authoritative rollback source — captured and committed
                # *before* the live mutation, so a failure after a
                # successful Drive write can never leave a mutation with no
                # way back.
                pre_state = self._pre_mutation_state(step.action_type, current)
                self._rollback_records.create(execution_step_id=step.id, pre_state=pre_state)
                self._db.commit()
                progress = _StepProgress.NOT_APPLIED
            else:
                progress = self._observe_progress(step, current, record.pre_state)

            if progress is _StepProgress.CONFLICT:
                raise ConflictError(
                    "The file matches neither its state before nor after this step, so it "
                    "was changed by someone else — refusing to overwrite that change."
                )
            if progress is _StepProgress.APPLIED:
                already_applied = True
                self._reconcile_local_mirror(step, file)
            else:
                assert current is not None
                self._apply_action(step, adapter=adapter, file=file, current=current)
        except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the job
            logger.exception("execution_step_failed", extra={"execution_step_id": str(step.id)})
            self._db.rollback()
            self._steps.mark_failed(step)
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.FAILED,
                verification_status=VerificationStatus.SKIPPED,
                error=str(exc),
                executed_at=now,
                verified_at=None,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_failed",
                message=str(exc),
                metadata={
                    "execution_step_id": str(step.id),
                    "action_type": step.action_type,
                    "error_type": type(exc).__name__,
                },
            )
            self._db.commit()
            return False

        if already_applied:
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_already_applied",
                message="An earlier attempt had already made this change; it was not repeated.",
                metadata={"execution_step_id": str(step.id), "action_type": step.action_type},
            )

        # The mutation itself succeeded — a verification problem is
        # recorded, not treated as step failure (Phase 8 spec: "Verification
        # failures should trigger recovery procedures," not undo a
        # successful action retroactively).
        try:
            verified = self._verify_forward(step, adapter=adapter, file=file)
            verification_status = (
                VerificationStatus.VERIFIED if verified else VerificationStatus.FAILED
            )
        except Exception:  # noqa: BLE001 - verification itself failing is not a step failure
            logger.exception(
                "execution_step_verification_failed", extra={"execution_step_id": str(step.id)}
            )
            verification_status = VerificationStatus.FAILED

        self._steps.mark_completed(step)
        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.SUCCESS,
            verification_status=verification_status,
            error=None,
            executed_at=now,
            verified_at=datetime.now(UTC),
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="step_completed",
            metadata={
                "execution_step_id": str(step.id),
                "action_type": step.action_type,
                "verification_status": verification_status,
            },
        )
        self._db.commit()
        return True

    def _get_plan_file(self, plan: ExecutionPlan, step: ExecutionStep) -> File:
        """Every mutation is scoped to the plan's own organization here, at
        the point of mutation. Plan creation already checks ownership, but
        the engine must not trust that a step's file id was vetted: one wrong
        id would otherwise act on another tenant's inventory. The same
        not-found error covers 'missing' and 'someone else's', so nothing
        reveals that another organization's file exists."""
        owned = self._files.get_owned_with_connector(
            step.target_file_id, organization_id=plan.organization_id
        )
        if owned is None or owned[1].provider != plan.target_provider:
            raise NotFoundError("Target file no longer exists in this platform's inventory.")
        return owned[0]

    @staticmethod
    def _reject_if_stale(step: ExecutionStep, current: StorageFile) -> None:
        """A plan reviewed against one version of a file must not be applied
        to a different one. The plan records the file's revision at creation
        (`step.pre_state["revision"]`); if the provider now reports a
        different content revision, someone edited the file since and the
        step is refused rather than applied blindly. Steps created before
        revisions were recorded, and providers that expose no content
        revision for an item, are not blocked — `Revision.differs_from` only
        declares a difference when both sides carry a token."""
        expected = Revision.from_dict(step.pre_state.get("revision"))
        if expected is not None and expected.differs_from(current.revision):
            raise StorageStaleRevisionError(
                "The file was edited after this plan was created — refusing to change a "
                "version that was never reviewed.",
                provider=current.provider,
                provider_code="stale_revision",
            )

    @staticmethod
    def _observe_progress(
        step: ExecutionStep, current: StorageFile | None, pre_state: dict
    ) -> _StepProgress:
        """State-based retry detection — not 'swallow the error'. A retry
        may treat a step as already applied only when the live file is in
        the state the step produces; if it is still in the captured
        pre-state the step is simply not applied yet; anything else means
        somebody else changed the file in the meantime, and overwriting that
        would be a guess. (When another actor happened to reach the exact
        target state, the outcome is identical to ours and is reported as
        already applied — the audit trail says so.)"""
        action_type = step.action_type
        if action_type == ExecutionActionType.PERMANENT_DELETE:
            if current is None:
                return _StepProgress.APPLIED
            return _StepProgress.NOT_APPLIED if current.trashed else _StepProgress.CONFLICT
        if current is None:
            return _StepProgress.CONFLICT
        if action_type in _TRASHABLE_ACTIONS:
            return _StepProgress.APPLIED if current.trashed else _StepProgress.NOT_APPLIED
        if action_type == ExecutionActionType.RENAME:
            if current.name == step.planned_change.get("new_name"):
                return _StepProgress.APPLIED
            if current.name == pre_state.get("name"):
                return _StepProgress.NOT_APPLIED
            return _StepProgress.CONFLICT
        if action_type in _MOVE_ACTIONS:
            if step.planned_change.get("new_parent_id") in current.parent_ids:
                return _StepProgress.APPLIED
            if pre_state.get("parent_folder_id") in current.parent_ids:
                return _StepProgress.NOT_APPLIED
            return _StepProgress.CONFLICT
        # `appProperties` writes set fixed values, so repeating one is
        # already idempotent.
        return _StepProgress.NOT_APPLIED

    def _reconcile_local_mirror(self, step: ExecutionStep, file: File) -> None:
        """The provider write of a crashed earlier attempt landed but its local
        bookkeeping did not — bring the mirror in line without a second
        write."""
        if step.action_type in _TRASHABLE_ACTIONS:
            self._files.mark_trashed(file, trashed=True)
        elif (
            step.action_type == ExecutionActionType.PERMANENT_DELETE
            and file.permanently_deleted_at is None
        ):
            self._files.mark_permanently_deleted(file)

    @staticmethod
    def _pre_mutation_state(action_type: str, current: StorageFile) -> dict:
        if action_type in _TRASHABLE_ACTIONS:
            return {"trashed": False}
        if action_type == ExecutionActionType.RENAME:
            return {"name": current.name}
        if action_type in _MOVE_ACTIONS:
            return {"parent_folder_id": current.parent_id}
        return {}

    def _apply_action(
        self, step: ExecutionStep, *, adapter: StorageAdapter, file: File, current: StorageFile
    ) -> None:
        action_type = step.action_type
        file_id = ProviderFileId(file.provider_file_id)
        if action_type in _TRASHABLE_ACTIONS:
            adapter.trash(file_id)
            # The local mirror's only record that this file left active
            # storage — excluded from every Storage Intelligence/Dashboard
            # total from here on (FileRepository._for_organization etc.),
            # without deleting the row (see File.trashed's docstring on why).
            self._files.mark_trashed(file, trashed=True)
        elif action_type == ExecutionActionType.RENAME:
            new_name = step.planned_change.get("new_name")
            if not new_name:
                raise ValidationError("Rename step has no target name.")
            adapter.rename(file_id, new_name)
        elif action_type in _MOVE_ACTIONS:
            new_parent_id = step.planned_change.get("new_parent_id")
            old_parent_id = current.parent_id
            if not new_parent_id or not old_parent_id:
                raise ValidationError("Move step is missing a source or target parent folder.")
            adapter.move(
                file_id,
                new_parent_id=ProviderFileId(new_parent_id),
                old_parent_id=old_parent_id,
            )
        elif action_type == ExecutionActionType.UPDATE_METADATA:
            properties = step.planned_change.get("properties")
            if not properties:
                raise ValidationError("Update-metadata step has no properties to set.")
            adapter.update_metadata(file_id, properties)
        elif action_type == ExecutionActionType.PERMANENT_DELETE:
            adapter.permanent_delete(file_id)
            self._files.mark_permanently_deleted(file)
        else:
            raise ValidationError(f"Unsupported action type: {action_type}")

    # ------------------------------------------------------------------
    # Archive MVP — one batch operation completing N CREATE_ARCHIVE steps
    # ------------------------------------------------------------------

    def _fetch_file_bytes(
        self, file: File, *, adapter: StorageAdapter, max_bytes: int
    ) -> bytes | None:
        """Returns `None` for a file this archive can't meaningfully
        contain — a provider-native document with no exportable format
        (forms, sites, drawings) or a folder. Callers treat that as an
        individual step failure, not a batch failure. Reads are bounded by
        `max_bytes`, so a file larger than its recorded size (or than the
        cap) is abandoned mid-transfer, never loaded whole."""
        mime = file.mime_type or ""
        file_id = ProviderFileId(file.provider_file_id)
        export_format = adapter.export_format_for(mime, ExportPurpose.ARCHIVE)
        if export_format is not None:
            return read_bounded(adapter.export(file_id, export_format), max_bytes=max_bytes)
        if adapter.is_native_document(mime):
            return None
        return read_bounded(adapter.open_read(file_id), max_bytes=max_bytes)

    def _fail_archive_step(
        self, job: ExecutionJob, plan: ExecutionPlan, step: ExecutionStep, error: str
    ) -> None:
        now = datetime.now(UTC)
        self._steps.mark_failed(step)
        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.FAILED,
            verification_status=VerificationStatus.SKIPPED,
            error=error,
            executed_at=now,
            verified_at=None,
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="step_failed",
            message=error,
            metadata={"execution_step_id": str(step.id), "action_type": step.action_type},
        )

    def _execute_archive_batch(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        adapter: StorageAdapter,
    ) -> None:
        """Completes every `CREATE_ARCHIVE` step in `steps` together as one
        zip → object-storage write, not independently. Per-file problems
        (missing file, oversized, unexportable type, fetch error) fail only
        that one step and continue; a failure in the shared part of the
        operation (nothing left to archive, or the object-storage write
        itself) fails the whole batch as a group — see the except block
        below for why that's `rollback()`-then-refail rather than leaving
        already-failed steps alone."""
        settings = get_settings()
        archive_job = self._archive_jobs.get_by_plan_id(plan.id) or self._archive_jobs.create(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            name=f"Archive - {plan.estimated_impact}",
            created_by_user_id=plan.created_by_user_id,
        )
        self._archive_jobs.mark_creating(archive_job)
        self._db.commit()

        manifest: list[dict] = []
        included_steps: list[ExecutionStep] = []
        # Closed in the `finally` below (and by boto3 on upload) — a `with` block
        # here would re-indent the whole batch for no behavioral gain.
        buffer = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)  # noqa: SIM115
        try:
            try:
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    used_names: set[str] = set()
                    for step in steps:
                        try:
                            file = self._get_plan_file(plan, step)
                        except NotFoundError:
                            file = None
                        if file is None:
                            self._fail_archive_step(
                                job,
                                plan,
                                step,
                                "Target file no longer exists in this platform's inventory.",
                            )
                            continue
                        if (file.size_bytes or 0) > settings.archive_max_file_size_bytes:
                            self._fail_archive_step(
                                job, plan, step, "File is too large to archive."
                            )
                            continue

                        try:
                            content = self._fetch_file_bytes(
                                file,
                                adapter=adapter,
                                max_bytes=settings.archive_max_file_size_bytes,
                            )
                        except Exception as exc:  # noqa: BLE001 - one file's fetch failure must not stop the batch
                            self._fail_archive_step(job, plan, step, str(exc))
                            continue
                        if content is None:
                            self._fail_archive_step(
                                job, plan, step, "This file type can't be archived."
                            )
                            continue

                        zip_name = file.name
                        suffix = 1
                        while zip_name in used_names:
                            suffix += 1
                            zip_name = f"{file.name} ({suffix})"
                        used_names.add(zip_name)

                        zip_file.writestr(zip_name, content)
                        manifest.append(
                            {
                                "file_id": str(file.id),
                                "name": file.name,
                                "path": file.path,
                                "size_bytes": len(content),
                                "mime_type": file.mime_type,
                                "checksum_sha256": hashlib.sha256(content).hexdigest(),
                            }
                        )
                        included_steps.append(step)

                if not manifest:
                    raise ValidationError("No selected file could be included in the archive.")

                original_size = sum(entry["size_bytes"] for entry in manifest)
                # boto3's upload_fileobj closes the passed file object once
                # the transfer completes — compressed_size has to be read
                # *before* that call, not after; put_object must be the
                # last thing this method ever does to `buffer`.
                compressed_size = buffer.seek(0, 2)
                buffer.seek(0)
                key = f"archives/{plan.organization_id}/{plan.id}.zip"
                self._object_storage.put_object(
                    key=key, body=buffer, content_type="application/zip"
                )
            except Exception as exc:  # noqa: BLE001 - archive-batch boundary must never crash the worker
                logger.exception("archive_batch_failed", extra={"execution_plan_id": str(plan.id)})
                # rollback() expires every object in the session, so any
                # individual _fail_archive_step calls already flushed above
                # (but never committed) are undone too — every step in
                # `steps` reads back as PENDING again here. That's the
                # desired failure semantics for this action type: one
                # object-storage write failing after some files fetched
                # fine should fail the whole batch together, not partially
                # (see docstring above on _execute_archive_batch).
                self._db.rollback()
                self._archive_jobs.mark_failed(archive_job)
                for step in steps:
                    self._fail_archive_step(job, plan, step, str(exc))
                self._db.commit()
                return
        finally:
            buffer.close()

        self._archive_jobs.mark_completed(
            archive_job,
            object_storage_key=key,
            original_size_bytes=original_size,
            compressed_size_bytes=compressed_size,
            file_count=len(manifest),
            manifest=manifest,
        )

        now = datetime.now(UTC)
        for step in included_steps:
            self._rollback_records.create(
                execution_step_id=step.id, pre_state={"archive_job_id": str(archive_job.id)}
            )
            self._steps.mark_completed(step)
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.SUCCESS,
                verification_status=VerificationStatus.VERIFIED,
                error=None,
                executed_at=now,
                verified_at=now,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="step_completed",
                metadata={
                    "execution_step_id": str(step.id),
                    "action_type": step.action_type,
                    "archive_job_id": str(archive_job.id),
                },
            )
        self._db.commit()

    def _verify_forward(self, step: ExecutionStep, *, adapter: StorageAdapter, file: File) -> bool:
        file_id = ProviderFileId(file.provider_file_id)
        if step.action_type == ExecutionActionType.PERMANENT_DELETE:
            # The success signal here is the opposite of every other
            # action's: "not found" *is* verification, not a failure.
            try:
                adapter.get_file(file_id)
            except NotFoundError:
                return True
            return False

        current = adapter.get_file(file_id)
        if step.action_type in _TRASHABLE_ACTIONS:
            return current.trashed is True
        if step.action_type == ExecutionActionType.RENAME:
            expected = step.planned_change.get("new_name")
            return expected is None or current.name == expected
        if step.action_type in _MOVE_ACTIONS:
            expected = step.planned_change.get("new_parent_id")
            return expected is None or expected in current.parent_ids
        return True

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _run_rollback(
        self, job: ExecutionJob, plan: ExecutionPlan, *, adapter: StorageAdapter
    ) -> None:
        records = self._rollback_records.list_rollbackable_for_plan(plan.id)
        succeeded = 0
        failed = 0
        trashable_restored = False
        for record in records:
            self._check_cancelled(job.id)
            if self._jobs.is_pause_requested(job.id):
                self._jobs.mark_paused(job)
                self._audits.record(
                    organization_id=plan.organization_id,
                    execution_plan_id=plan.id,
                    execution_job_id=job.id,
                    event_type="execution_paused",
                )
                self._db.commit()
                raise ExecutionPaused

            step = self._steps.get_by_id(record.execution_step_id)
            if step is None:
                failed += 1
                continue

            if self._rollback_one(job, plan, step, record, adapter=adapter):
                succeeded += 1
                if step.action_type in _TRASHABLE_ACTIONS:
                    trashable_restored = True
            else:
                failed += 1

        if failed == 0 and succeeded > 0:
            self._jobs.mark_completed(job)
            self._plans.update_status(plan, status=ExecutionPlanStatus.ROLLED_BACK)
        elif succeeded == 0:
            self._jobs.mark_failed(job, error="All rollback steps failed.")
        else:
            self._jobs.mark_partially_completed(job)

        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="execution_rollback_completed",
            metadata={"succeeded": succeeded, "failed": failed},
        )
        self._db.commit()

        # Un-trashing restores the file to active storage — the totals need
        # to come back up, same as they needed to go down when it was
        # trashed in the first place.
        if trashable_restored:
            self._trigger_storage_refresh(plan.organization_id)

    def _rollback_one(
        self,
        job: ExecutionJob,
        plan: ExecutionPlan,
        step: ExecutionStep,
        record: RollbackRecord,
        *,
        adapter: StorageAdapter,
    ) -> bool:
        now = datetime.now(UTC)
        try:
            file = self._get_plan_file(plan, step)

            self._apply_rollback(step, adapter=adapter, file=file, pre_state=record.pre_state)
            self._rollback_records.mark_rolled_back(record, user_id=job.triggered_by_user_id)
            self._steps.mark_rolled_back(step)
        except Exception as exc:  # noqa: BLE001 - one file's rollback failure must not stop the rest
            logger.exception(
                "execution_rollback_step_failed", extra={"execution_step_id": str(step.id)}
            )
            self._db.rollback()
            self._results.create(
                execution_job_id=job.id,
                execution_step_id=step.id,
                status=ExecutionResultStatus.FAILED,
                verification_status=VerificationStatus.SKIPPED,
                error=str(exc),
                executed_at=now,
                verified_at=None,
            )
            self._audits.record(
                organization_id=plan.organization_id,
                execution_plan_id=plan.id,
                execution_job_id=job.id,
                event_type="rollback_step_failed",
                message=str(exc),
                metadata={"execution_step_id": str(step.id)},
            )
            self._db.commit()
            return False

        self._results.create(
            execution_job_id=job.id,
            execution_step_id=step.id,
            status=ExecutionResultStatus.SUCCESS,
            verification_status=VerificationStatus.VERIFIED,
            error=None,
            executed_at=now,
            verified_at=datetime.now(UTC),
        )
        self._audits.record(
            organization_id=plan.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            event_type="rollback_step_completed",
            metadata={"execution_step_id": str(step.id)},
        )
        self._db.commit()
        return True

    def _apply_rollback(
        self, step: ExecutionStep, *, adapter: StorageAdapter, file: File, pre_state: dict
    ) -> None:
        """Rollback is retryable and never clobbers a third party's change:
        a file already in its pre-state is left alone (a crashed earlier
        rollback attempt), and one that is neither in its pre-state nor in
        the state this step produced was changed by someone else since."""
        action_type = step.action_type
        file_id = ProviderFileId(file.provider_file_id)
        if action_type in _TRASHABLE_ACTIONS:
            adapter.restore(file_id)
            self._files.mark_trashed(file, trashed=False)
        elif action_type == ExecutionActionType.RENAME:
            current = adapter.get_file(file_id)
            if current.name == pre_state["name"]:
                return
            if current.name != step.planned_change.get("new_name"):
                raise ConflictError(
                    "The file was renamed by someone else after this step — refusing to overwrite."
                )
            adapter.rename(file_id, pre_state["name"])
        elif action_type in _MOVE_ACTIONS:
            current = adapter.get_file(file_id)
            current_parent_id = current.parent_id
            original_parent_id = pre_state.get("parent_folder_id")
            if not current_parent_id or not original_parent_id:
                raise ValidationError("Cannot determine parents to reverse this move.")
            if original_parent_id in current.parent_ids:
                return
            if step.planned_change.get("new_parent_id") not in current.parent_ids:
                raise ConflictError(
                    "The file was moved by someone else after this step — refusing to overwrite."
                )
            adapter.move(
                file_id,
                new_parent_id=ProviderFileId(original_parent_id),
                old_parent_id=current_parent_id,
            )
        elif action_type == ExecutionActionType.UPDATE_METADATA:
            adapter.update_metadata(file_id, pre_state.get("app_properties", {}))
        elif action_type == ExecutionActionType.CREATE_ARCHIVE:
            # No provider call — CREATE_ARCHIVE never mutated the provider,
            # only read from it, so `file` isn't touched here at all.
            self._rollback_archive(pre_state["archive_job_id"])
        elif action_type == ExecutionActionType.PERMANENT_DELETE:
            # Unreachable in practice — every permanent-delete plan is
            # created with rollback_available=False, and
            # ExecutionJobService.trigger_rollback rejects the request
            # before a job (and this call) ever exists. Kept as an
            # explicit, honest failure rather than silently no-op-ing.
            raise ValidationError("Permanently deleted files cannot be restored.")
        else:
            raise ValidationError(f"Unsupported action type: {action_type}")

    def _rollback_archive(self, archive_job_id: str) -> None:
        """Every sibling `CREATE_ARCHIVE` step in a plan shares one
        `ArchiveJob`, so this runs once per step during a plan rollback —
        idempotent via the status check, since the object is only ever
        deleted once."""
        archive_job = self._archive_jobs.get_by_id(uuid.UUID(archive_job_id))
        if archive_job is None or archive_job.status != ArchiveJobStatus.COMPLETED:
            return
        if archive_job.object_storage_key:
            self._object_storage.delete_object(key=archive_job.object_storage_key)
        self._archive_jobs.mark_failed(archive_job)

    def _check_cancelled(self, execution_job_id: uuid.UUID) -> None:
        if self._jobs.is_cancel_requested(execution_job_id):
            raise ExecutionCancelled

    def _trigger_storage_refresh(self, organization_id: uuid.UUID) -> None:
        """A trash (or its rollback) just changed which files count toward
        active storage — without this, Storage Intelligence's totals and
        the Dashboard's `total_storage_bytes` would keep reporting the
        pre-trash number until the next scan or manual re-analysis (see
        `File.trashed`'s docstring). Mirrors the exact enqueue-guarded-by-
        has_active_job pattern `worker.tasks.scan`/`worker.tasks.embedding`
        already use for their own completion-chained triggers — a failure
        to enqueue either (e.g. one already running) is not itself a
        problem worth failing the execution job over, so both are best-
        effort here."""
        if not self._storage_analysis_jobs.has_active_job(organization_id):
            analysis_job = self._storage_analysis_jobs.create(
                organization_id=organization_id,
                triggered_by=StorageAnalysisTrigger.EXECUTION_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            run_storage_intelligence.delay(str(analysis_job.id))

        if not self._recommendation_jobs.has_active_job(organization_id):
            recommendation_job = self._recommendation_jobs.create(
                organization_id=organization_id,
                triggered_by=RecommendationTrigger.EXECUTION_COMPLETED,
                triggered_by_user_id=None,
            )
            self._db.commit()
            run_recommendation.delay(str(recommendation_job.id))
