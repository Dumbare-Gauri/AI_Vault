import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import ExecutionAudit


class ExecutionAuditRepository:
    """Append-only — mirrors every other `*Event`/`*Audit` repository's
    defensive `message[:1024]` truncation (the fix for a real crash-cascade
    bug first found in Phases 4-5: an unbounded exception string overflowing
    this exact column shape)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        organization_id: uuid.UUID,
        execution_plan_id: uuid.UUID | None = None,
        execution_job_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        event_type: str,
        message: str | None = None,
        metadata: dict | None = None,
    ) -> ExecutionAudit:
        entry = ExecutionAudit(
            organization_id=organization_id,
            execution_plan_id=execution_plan_id,
            execution_job_id=execution_job_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            message=message[:1024] if message else None,
            metadata_=metadata or {},
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def list_for_plan(self, execution_plan_id: uuid.UUID) -> list[ExecutionAudit]:
        return (
            self._session.query(ExecutionAudit)
            .filter_by(execution_plan_id=execution_plan_id)
            .order_by(ExecutionAudit.created_at)
            .all()
        )

    def list_for_job(self, execution_job_id: uuid.UUID) -> list[ExecutionAudit]:
        return (
            self._session.query(ExecutionAudit)
            .filter_by(execution_job_id=execution_job_id)
            .order_by(ExecutionAudit.created_at)
            .all()
        )

    def list_events_for_organization(
        self, organization_id: uuid.UUID, *, event_types: list[str], limit: int
    ) -> list[ExecutionAudit]:
        return (
            self._session.query(ExecutionAudit)
            .filter(
                ExecutionAudit.organization_id == organization_id,
                ExecutionAudit.event_type.in_(event_types),
            )
            .order_by(ExecutionAudit.created_at.desc())
            .limit(limit)
            .all()
        )
