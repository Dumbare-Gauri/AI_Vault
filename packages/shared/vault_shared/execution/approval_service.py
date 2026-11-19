import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from vault_shared import ConflictError, NotFoundError, ValidationError, VaultError
from vault_shared.db.models import (
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
    StorageConnector,
    WorkflowPolicy,
)
from vault_shared.db.repositories import (
    ApprovalDecisionRepository,
    ApprovalRequestRepository,
    AuditLogRepository,
    ConnectorCredentialsRepository,
    ExecutionAuditRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    StorageConnectorRepository,
    WorkflowNodeExecutionRepository,
)
from vault_shared.execution.permission_validation import validate_execution_permissions

_EVENT_TYPE_BY_DECISION: dict[str, str] = {
    ApprovalDecisionType.APPROVE: "execution_approved",
    ApprovalDecisionType.REJECT: "execution_rejected",
    ApprovalDecisionType.REQUEST_CHANGES: "execution_changes_requested",
}
_PLAN_STATUS_BY_DECISION: dict[str, str] = {
    ApprovalDecisionType.APPROVE: ExecutionPlanStatus.APPROVED,
    ApprovalDecisionType.REJECT: ExecutionPlanStatus.REJECTED,
    ApprovalDecisionType.REQUEST_CHANGES: ExecutionPlanStatus.CHANGES_REQUESTED,
}
_APPROVAL_STATUS_BY_DECISION: dict[str, str] = {
    ApprovalDecisionType.APPROVE: ApprovalStatus.APPROVED,
    ApprovalDecisionType.REJECT: ApprovalStatus.REJECTED,
    ApprovalDecisionType.REQUEST_CHANGES: ApprovalStatus.CHANGES_REQUESTED,
}


class ApprovalService:
    """The Approval System (Phase 8 spec, Handbook §13's "Approval
    workflow for destructive actions... explicit human approval captured
    before execution"). `decide()` is the single choke point every
    execution passes through — only an `approve` decision that also
    passes `validate_execution_permissions` creates an `ExecutionJob` and
    enqueues it; `reject`/`request_changes` are terminal for this plan.

    ADR-026 (Phase 2): a direct, user-triggered `ExecutionPlan` no longer
    creates an `ApprovalRequest` at all (see
    `plan_service.ExecutionPlanService`'s `require_approval` parameter),
    so this class's remaining callers are exclusively the Automation
    Engine's own `WorkflowPolicy` effects (`auto_decide_via_policy` for
    `AUTO_EXECUTE`, `decide()` for a human resolving a `REQUIRE_APPROVAL`
    node) and plain workflow `APPROVAL` nodes with no `execution_plan_id`
    at all — a distinct, still-real concept ("a human must review this
    automated workflow's step") from the one-off approval step this
    product no longer has.

    Lives in `packages/shared`, not `apps/backend`, since Phase 9's
    `EXECUTE_ACTION` workflow node (running in `apps/worker`) needs
    `auto_decide_via_policy` too — same "promote once a second app needs
    it" reasoning as `plan_service.ExecutionPlanService`. Enqueue callbacks
    are constructor-injected (the `GoogleWorkspaceOAuthClient`/`AIGateway`
    provider-selection pattern) rather than importing either app's Celery
    producer directly: `apps/backend` injects its `app.infrastructure.
    queue.*` producer functions; `apps/worker` injects direct
    `celery_app.send_task` calls — neither app imports the other's code.

    Phase 9 (ADR-021): also handles a workflow-node-scoped `ApprovalRequest`
    (no `execution_plan_id`, e.g. a plain `APPROVAL` node) — same decision
    recording, but resumes the paused `WorkflowExecution` instead of
    creating an `ExecutionJob`. `auto_decide_via_policy` is the second,
    non-human decider path a published `WorkflowPolicy` uses for an
    `EXECUTE_ACTION` node's `AUTO_EXECUTE` effect — it is not a bypass of
    this approval system, it is a second kind of approver, producing the
    exact same `ApprovalDecision`/`ExecutionJob`/audit trail a human's
    `decide()` call would, just attributed to a policy instead of a user."""

    def __init__(
        self,
        db: Session,
        *,
        enqueue_execution_job: Callable[[uuid.UUID], None],
        enqueue_workflow_execution: Callable[[uuid.UUID], None],
    ) -> None:
        self._db = db
        self._approvals = ApprovalRequestRepository(db)
        self._decisions = ApprovalDecisionRepository(db)
        self._plans = ExecutionPlanRepository(db)
        self._jobs = ExecutionJobRepository(db)
        self._connectors = StorageConnectorRepository(db)
        self._credentials = ConnectorCredentialsRepository(db)
        self._workflow_node_executions = WorkflowNodeExecutionRepository(db)
        self._execution_audits = ExecutionAuditRepository(db)
        self._audit_logs = AuditLogRepository(db)
        self._enqueue_execution_job = enqueue_execution_job
        self._enqueue_workflow_execution = enqueue_workflow_execution

    def list_for_organization(
        self, organization_id: uuid.UUID, *, status: str | None = None
    ) -> list[ApprovalRequest]:
        requests = self._approvals.list_for_organization(organization_id, status=status)
        return [self._approvals.expire_if_overdue(request) for request in requests]

    def get_owned(
        self, approval_request_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> ApprovalRequest:
        request = self._approvals.get_owned(approval_request_id, organization_id=organization_id)
        if request is None:
            raise NotFoundError("Approval request not found.")
        return self._approvals.expire_if_overdue(request)

    def decide(
        self,
        approval_request_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        decision: str,
        comments: str | None,
        ip_address: str | None,
    ) -> ApprovalRequest:
        request = self.get_owned(approval_request_id, organization_id=organization_id)
        if request.status != ApprovalStatus.PENDING:
            raise ConflictError(f"This approval request is already {request.status}.")

        plan = None
        if request.execution_plan_id is not None:
            plan = self._plans.get_by_id(request.execution_plan_id)
            if plan is None:
                raise NotFoundError("Execution plan not found.")
            if decision == ApprovalDecisionType.APPROVE:
                self._check_permissions(plan, organization_id=organization_id)

        self._decisions.create(
            approval_request_id=request.id,
            decider_user_id=user_id,
            decision=decision,
            comments=comments,
            ip_address=ip_address,
        )
        self._approvals.mark_status(request, status=_APPROVAL_STATUS_BY_DECISION[decision])

        job = None
        if plan is not None:
            self._plans.update_status(plan, status=_PLAN_STATUS_BY_DECISION[decision])
            if decision == ApprovalDecisionType.APPROVE:
                job = self._jobs.create(
                    execution_plan_id=plan.id,
                    organization_id=organization_id,
                    triggered_by_user_id=user_id,
                )

        self._execution_audits.record(
            organization_id=organization_id,
            execution_plan_id=plan.id if plan else None,
            execution_job_id=job.id if job else None,
            actor_user_id=user_id,
            event_type=_EVENT_TYPE_BY_DECISION[decision],
            message=comments,
        )
        self._audit_logs.record(
            event_type=_EVENT_TYPE_BY_DECISION[decision],
            organization_id=organization_id,
            user_id=user_id,
            ip_address=ip_address,
            metadata={
                "approval_request_id": str(request.id),
                **({"execution_plan_id": str(plan.id)} if plan else {}),
            },
        )
        self._db.commit()

        if job is not None:
            self._enqueue_execution_job(job.id)
        self._resume_workflow_if_applicable(request)
        return request

    def auto_decide_via_policy(
        self, approval_request_id: uuid.UUID, *, policy: WorkflowPolicy
    ) -> ApprovalRequest:
        """The policy-attributed counterpart to `decide()` — used only by
        `worker.workflow.execution_service` for an `EXECUTE_ACTION` node
        whose policy effect is `AUTO_EXECUTE`. Always an `approve`: a
        policy that doesn't want to act simply never reaches this call
        (its `EXECUTE_ACTION` node either never builds a plan at all, for
        `SKIP`, or routes to a real human `ApprovalRequest`, for
        `REQUIRE_APPROVAL`) — there is no such thing as a policy
        "rejecting" a plan it just created."""
        request = self.get_owned(approval_request_id, organization_id=policy.organization_id)
        if request.status != ApprovalStatus.PENDING:
            raise ConflictError(f"This approval request is already {request.status}.")
        if request.execution_plan_id is None:
            raise ValidationError("A policy can only auto-decide a plan's approval.")

        plan = self._plans.get_by_id(request.execution_plan_id)
        if plan is None:
            raise NotFoundError("Execution plan not found.")
        self._check_permissions(plan, organization_id=policy.organization_id)

        self._decisions.create_by_policy(
            approval_request_id=request.id,
            decided_by_policy_id=policy.id,
            decision=ApprovalDecisionType.APPROVE,
        )
        self._approvals.mark_status(request, status=ApprovalStatus.APPROVED)
        self._plans.update_status(plan, status=ExecutionPlanStatus.APPROVED)
        job = self._jobs.create(
            execution_plan_id=plan.id,
            organization_id=policy.organization_id,
            triggered_by_user_id=None,
        )

        self._execution_audits.record(
            organization_id=policy.organization_id,
            execution_plan_id=plan.id,
            execution_job_id=job.id,
            actor_user_id=None,
            event_type="execution_approved",
            metadata={"decided_by_policy_id": str(policy.id), "policy_key": policy.policy_key},
        )
        self._audit_logs.record(
            event_type="execution_approved",
            organization_id=policy.organization_id,
            metadata={
                "execution_plan_id": str(plan.id),
                "approval_request_id": str(request.id),
                "decided_by_policy_id": str(policy.id),
            },
        )
        self._db.commit()

        self._enqueue_execution_job(job.id)
        self._resume_workflow_if_applicable(request)
        return request

    def bulk_decide(
        self,
        approval_request_ids: list[uuid.UUID],
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        decision: str,
        comments: str | None,
        ip_address: str | None,
    ) -> list[ApprovalRequest]:
        """One bad id in the batch must not block the rest — same per-item
        failure isolation every job-processing loop in this codebase has
        used since Phase 4."""
        decided = []
        for request_id in approval_request_ids:
            try:
                decided.append(
                    self.decide(
                        request_id,
                        organization_id=organization_id,
                        user_id=user_id,
                        decision=decision,
                        comments=comments,
                        ip_address=ip_address,
                    )
                )
            except VaultError:
                continue
        return decided

    def _check_permissions(self, plan: ExecutionPlan, *, organization_id: uuid.UUID) -> None:
        connector = self._resolve_connector(plan, organization_id=organization_id)
        credentials = self._credentials.get_by_connector_id(connector.id) if connector else None
        failures = validate_execution_permissions(connector=connector, credentials=credentials)
        if failures:
            raise ValidationError(
                "Cannot approve — execution permissions are not satisfied: "
                + "; ".join(failures)
            )

    def _resolve_connector(
        self, plan: ExecutionPlan, *, organization_id: uuid.UUID
    ) -> StorageConnector | None:
        return self._connectors.get_by_organization_and_provider(
            organization_id=organization_id, provider=plan.target_provider
        )

    def _resume_workflow_if_applicable(self, request: ApprovalRequest) -> None:
        if request.workflow_node_execution_id is None:
            return
        node_execution = self._workflow_node_executions.get_by_id(
            request.workflow_node_execution_id
        )
        if node_execution is not None:
            self._enqueue_workflow_execution(node_execution.workflow_execution_id)
