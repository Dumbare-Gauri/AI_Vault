"""Thin subclass — `ExecutionPlanService` moved to `packages/shared` in
Phase 9 (ADR-021) so `apps/worker`'s `EXECUTE_ACTION` workflow node can call
it directly, not just this backend's own `POST /v1/execution-plans`
endpoint. This subclass exists only to inject the backend's own Celery
producer as `enqueue_execution_job`, which the shared class needs for its
`require_approval=False` path (ADR-026) — the worker's own call site
constructs the shared class directly instead, since it never uses that
path. See `vault_shared.execution.plan_service` for the real
implementation."""

from sqlalchemy.orm import Session

from app.infrastructure.queue.execution_producer import enqueue_execution_job
from vault_shared.execution import ExecutionPlanDetail
from vault_shared.execution import ExecutionPlanService as _SharedExecutionPlanService

__all__ = ["ExecutionPlanDetail", "ExecutionPlanService"]


class ExecutionPlanService(_SharedExecutionPlanService):
    def __init__(self, db: Session) -> None:
        super().__init__(db, enqueue_execution_job=enqueue_execution_job)
