import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vault_shared.db.session import Base


class ExecutionActionType(enum.StrEnum):
    """The Phase 8 spec's "Supported initial actions" list, plus two later
    additions (`CREATE_ARCHIVE`, `PERMANENT_DELETE`). `ARCHIVE`/
    `REMOVE_DUPLICATE` use Drive's own recoverable Trash (see
    `GoogleDriveClient.set_trashed`) — `PERMANENT_DELETE` is the one
    exception that calls real `files.delete`, and is deliberately never
    reachable from the generic ad-hoc plan path or instant auto-approval
    (see `ExecutionPlanService.create_permanent_delete_plan` and the
    execution-plans router's dedicated endpoint for it)."""

    MOVE_FILE = "move_file"
    MOVE_FOLDER = "move_folder"
    RENAME = "rename"
    ARCHIVE = "archive"
    REMOVE_DUPLICATE = "remove_duplicate"
    UPDATE_METADATA = "update_metadata"
    # Archive MVP — unlike every action above, this doesn't mutate Drive at
    # all (files are only read/downloaded); N steps in one plan are
    # completed together by one batch operation in ExecutionService rather
    # than independently, see _execute_archive_batch.
    CREATE_ARCHIVE = "create_archive"
    # Real, unrecoverable Drive deletion — only reachable from a file
    # that's already `trashed`, never auto-approved, never rollback-able.
    PERMANENT_DELETE = "permanent_delete"
    # Un-trash a file — the inverse of ARCHIVE/REMOVE_DUPLICATE, only
    # reachable for a file that is currently `trashed`.
    RESTORE = "restore"


class ExecutionStepStatus(enum.StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class ExecutionStep(Base):
    """One ordered, single-file operation within an `ExecutionPlan`. Every
    current action type targets a `File` row — `MOVE_FOLDER` is a
    supported `ExecutionActionType` per the phase spec, but no
    Recommendation rule produces folder-level plans yet (Phase 7's 11
    rules all operate on files); the connector call underneath
    (`GoogleDriveClient.move_file`) already works identically for a
    folder id, so this isn't a design gap, just an unexercised path.

    `pre_state` is captured at *plan-creation* time — for the founder's
    review context ("here's this file's current name/location"), not the
    authoritative rollback source. That's `RollbackRecord.pre_state`,
    captured fresh immediately before the actual mutating call at
    execution time, since a file's real state can drift between plan
    creation and (eventually approved) execution."""

    __tablename__ = "execution_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    execution_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pre_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    planned_change: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionStepStatus.PENDING
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
