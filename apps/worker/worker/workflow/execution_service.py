import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from vault_shared import ValidationError, VaultError, get_logger, get_settings
from vault_shared.ai_gateway import get_ai_gateway
from vault_shared.ai_gateway.interfaces import Message
from vault_shared.ai_gateway.org_completion_provider import resolve_org_completion_provider
from vault_shared.db.models import (
    ApprovalStatus,
    Recommendation,
    RecommendationStatus,
    WorkflowExecution,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowNodeExecutionStatus,
    WorkflowNodeType,
    WorkflowPolicy,
    WorkflowPolicyEffect,
)
from vault_shared.db.repositories import (
    ApprovalRequestRepository,
    AuditLogRepository,
    RecommendationRepository,
    UserRepository,
    WorkflowExecutionRepository,
    WorkflowNodeExecutionRepository,
    WorkflowNodeRepository,
    WorkflowPolicyRepository,
    WorkflowRepository,
    WorkflowVersionRepository,
)
from vault_shared.execution import ApprovalService, ExecutionPlanService
from vault_shared.execution.plan_service import rule_removes_files
from vault_shared.notifications import NotificationDispatcher, get_email_provider

logger = get_logger("worker.workflow.execution_service")

_WAITING = "__waiting__"
_RESOLVED_STATUS_OUTCOME: dict[str, str] = {
    ApprovalStatus.APPROVED: "approved",
    ApprovalStatus.REJECTED: "rejected",
    ApprovalStatus.CHANGES_REQUESTED: "changes_requested",
    ApprovalStatus.EXPIRED: "rejected",
}


class WorkflowExecutionCancelled(Exception):
    """Unwinds `run()` once cooperative cancellation has been observed —
    mirrors `ExecutionCancelled` from Phase 8's `ExecutionService`."""


class WorkflowExecutionService:
    """The Workflow Execution Engine (Phase 9 spec) — walks a published
    `WorkflowVersion`'s node graph one node at a time, persisting state
    after every step so a crash or a deliberate pause/approval-wait can
    always resume from exactly where it left off (`WorkflowExecution.
    current_node_id`/`.context`). Never performs a Drive mutation itself —
    an `EXECUTE_ACTION` node only ever calls the same `ExecutionPlanService`/
    `ApprovalService` Phase 8 already built and tested (now living in
    `packages/shared`, see ADR-021), which in turn only ever *enqueues* a
    separate `worker.execution.run` job; this class's own job ends the
    moment a plan is approved (human or policy), not when it finishes
    executing."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._workflows = WorkflowRepository(db)
        self._versions = WorkflowVersionRepository(db)
        self._nodes = WorkflowNodeRepository(db)
        self._executions = WorkflowExecutionRepository(db)
        self._node_executions = WorkflowNodeExecutionRepository(db)
        self._policies = WorkflowPolicyRepository(db)
        self._approvals = ApprovalRequestRepository(db)
        self._recommendations = RecommendationRepository(db)
        self._users = UserRepository(db)
        self._audit_logs = AuditLogRepository(db)
        self._plan_service = ExecutionPlanService(db)
        self._approval_service = ApprovalService(
            db,
            enqueue_execution_job=self._enqueue_execution_job,
            enqueue_workflow_execution=self._enqueue_workflow_execution,
        )
        self._notifications = NotificationDispatcher(db, email_provider=get_email_provider())
        self._ai_gateway = get_ai_gateway()

    def run(self, workflow_execution_id: uuid.UUID) -> None:
        execution = self._executions.get_by_id(workflow_execution_id)
        if execution is None:
            logger.warning(
                "workflow_execution_not_found",
                extra={"workflow_execution_id": str(workflow_execution_id)},
            )
            return

        # An org's own AI provider config (if any) takes over for this
        # execution's AI-evaluation node calls — resolved once, up front,
        # for consistency with `ConversationService`/`IntelligenceService`
        # ("the org's own key powers all AI features"). One
        # `WorkflowExecutionService` instance serves exactly one `run()`
        # call (a fresh instance per Celery task), so reassigning
        # `self._ai_gateway` here is safe.
        org_completion_provider = resolve_org_completion_provider(
            self._db, execution.organization_id
        )
        if org_completion_provider is not None:
            self._ai_gateway = self._ai_gateway.with_completion_provider(org_completion_provider)

        version = self._versions.get_by_id(execution.workflow_version_id)
        if version is None:
            self._executions.mark_failed(execution, error="Workflow version no longer exists.")
            self._db.commit()
            return
        nodes_by_id = {node.id: node for node in self._nodes.list_for_version(version.id)}

        self._executions.mark_running(execution)
        self._audit_logs.record(
            event_type="workflow_execution_started",
            organization_id=execution.organization_id,
            metadata={"workflow_execution_id": str(execution.id)},
        )
        self._db.commit()

        current_node_id = execution.current_node_id
        if current_node_id is None:
            trigger_node = next(
                (n for n in nodes_by_id.values() if n.node_type == WorkflowNodeType.TRIGGER), None
            )
            if trigger_node is None:
                self._executions.mark_failed(execution, error="Workflow has no trigger node.")
                self._db.commit()
                return
            current_node_id = trigger_node.id

        context = dict(execution.context)

        try:
            while current_node_id is not None:
                self._check_cancelled(execution.id)
                if self._executions.is_pause_requested(execution.id):
                    self._executions.set_current_node(execution, node_id=current_node_id)
                    self._executions.set_context(execution, context=context)
                    self._executions.mark_paused(execution)
                    self._db.commit()
                    return

                node = nodes_by_id.get(current_node_id)
                if node is None:
                    raise ValidationError(
                        f"Node {current_node_id} no longer exists in this workflow version."
                    )

                outcome, context = self._process_node(execution, node, context=context)
                if outcome == _WAITING:
                    self._executions.set_current_node(execution, node_id=node.id)
                    self._executions.set_context(execution, context=context)
                    self._executions.mark_paused(execution)
                    self._db.commit()
                    return

                next_id = node.next_nodes.get(outcome) or node.next_nodes.get("default")
                current_node_id = uuid.UUID(next_id) if next_id else None

            self._executions.set_context(execution, context=context)
            self._executions.mark_completed(execution)
            self._audit_logs.record(
                event_type="workflow_execution_completed",
                organization_id=execution.organization_id,
                metadata={"workflow_execution_id": str(execution.id)},
            )
            self._db.commit()
        except WorkflowExecutionCancelled:
            self._executions.mark_cancelled(execution)
            self._db.commit()
        except Exception as exc:  # noqa: BLE001 - job execution boundary must never crash the worker
            logger.exception(
                "workflow_execution_failed", extra={"workflow_execution_id": str(execution.id)}
            )
            self._db.rollback()
            self._executions.mark_failed(execution, error=str(exc))
            self._db.commit()

    # ------------------------------------------------------------------
    # Node dispatch
    # ------------------------------------------------------------------

    def _process_node(
        self, execution: WorkflowExecution, node: WorkflowNode, *, context: dict
    ) -> tuple[str, dict]:
        node_execution = self._node_executions.get_for_execution_and_node(
            workflow_execution_id=execution.id, workflow_node_id=node.id
        )
        if node_execution is None:
            node_execution = self._node_executions.create(
                workflow_execution_id=execution.id, workflow_node_id=node.id
            )
        elif node_execution.status == WorkflowNodeExecutionStatus.WAITING_APPROVAL:
            return self._resume_waiting_approval(node_execution, context=context)
        elif node_execution.status == WorkflowNodeExecutionStatus.WAITING_DELAY:
            return self._resume_waiting_delay(node_execution, context=context)
        elif node_execution.status in (
            WorkflowNodeExecutionStatus.COMPLETED,
            WorkflowNodeExecutionStatus.SKIPPED,
        ):
            # Already decided in a prior run of this same execution
            # (shouldn't normally be re-visited, but resuming after a
            # crash mid-loop could re-enter here) — trust the recorded
            # outcome rather than re-running side effects a second time.
            return "default", context

        if node.node_type == WorkflowNodeType.TRIGGER:
            return self._handle_trigger(node_execution, context=context)
        if node.node_type == WorkflowNodeType.CONDITION:
            return self._handle_condition(node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.DECISION:
            return self._handle_decision(node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.AI_EVALUATION:
            return self._handle_ai_evaluation(node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.APPROVAL:
            return self._handle_approval(execution, node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.EXECUTE_ACTION:
            return self._handle_execute_action(execution, node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.DELAY:
            return self._handle_delay(execution, node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.NOTIFICATION:
            return self._handle_notification(execution, node, node_execution, context=context)
        if node.node_type == WorkflowNodeType.END:
            return self._handle_end(node_execution, context=context)

        raise ValidationError(f"Unsupported node type: {node.node_type}")

    # ------------------------------------------------------------------
    # Node handlers
    # ------------------------------------------------------------------

    def _handle_trigger(
        self, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        self._node_executions.mark_completed(node_execution, output_context={})
        return "default", context

    def _handle_condition(
        self, node: WorkflowNode, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        result = self._evaluate_condition(node.config, context)
        outcome = "true" if result else "false"
        self._node_executions.mark_completed(node_execution, output_context={"result": result})
        return outcome, context

    def _handle_decision(
        self, node: WorkflowNode, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        for branch in node.config.get("branches", []):
            if self._evaluate_condition(branch.get("condition", {}), context):
                outcome = branch.get("next", "default")
                self._node_executions.mark_completed(
                    node_execution, output_context={"matched": outcome}
                )
                return outcome, context
        self._node_executions.mark_completed(node_execution, output_context={"matched": "default"})
        return "default", context

    def _handle_ai_evaluation(
        self, node: WorkflowNode, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        prompt = self._render_template(node.config.get("prompt_template", ""), context)
        result = self._ai_gateway.complete(
            messages=[Message(role="user", content=prompt)], context=None
        )
        context_key = node.config.get("context_key", "ai_result")
        new_context = {**context, context_key: result.text}
        self._node_executions.mark_completed(
            node_execution, output_context={context_key: result.text, "provider": result.provider}
        )
        return "default", new_context

    def _handle_approval(
        self,
        execution: WorkflowExecution,
        node: WorkflowNode,
        node_execution: WorkflowNodeExecution,
        *,
        context: dict,
    ) -> tuple[str, dict]:
        workflow = self._workflows.get_by_id(execution.workflow_id)
        settings = get_settings()
        expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours)
        self._approvals.create(
            organization_id=execution.organization_id,
            requested_by_user_id=workflow.created_by_user_id,
            expires_at=expires_at,
            workflow_node_execution_id=node_execution.id,
        )
        self._node_executions.mark_waiting_approval(node_execution)
        self._notify_recipients(
            node.config,
            organization_id=execution.organization_id,
            workflow_execution_id=execution.id,
            default_subject="Workflow approval needed",
            default_body="A workflow is waiting for your approval.",
            context=context,
        )
        self._db.commit()
        return _WAITING, context

    def _resume_waiting_approval(
        self, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        approval_request = self._approvals.get_by_workflow_node_execution(node_execution.id)
        if approval_request is None:
            raise ValidationError("This node's approval request is missing.")
        approval_request = self._approvals.expire_if_overdue(approval_request)
        if approval_request.status == ApprovalStatus.PENDING:
            return _WAITING, context

        outcome = _RESOLVED_STATUS_OUTCOME.get(approval_request.status, "rejected")
        self._node_executions.mark_completed(
            node_execution, output_context={"approval_status": approval_request.status}
        )
        return outcome, context

    def _handle_execute_action(
        self,
        execution: WorkflowExecution,
        node: WorkflowNode,
        node_execution: WorkflowNodeExecution,
        *,
        context: dict,
    ) -> tuple[str, dict]:
        rule_name = node.config.get("rule_name")
        policy_key = node.config.get("policy_key")
        if not rule_name or not policy_key:
            self._node_executions.mark_skipped(
                node_execution, reason="Missing rule_name or policy_key configuration."
            )
            return "default", context

        policy = self._policies.get_published(
            organization_id=execution.organization_id, policy_key=policy_key
        )
        if policy is None:
            self._node_executions.mark_skipped(
                node_execution, reason=f"No published policy '{policy_key}'."
            )
            return "default", context

        recommendation = self._recommendations.get_by_rule(
            organization_id=execution.organization_id, rule_name=rule_name
        )
        if recommendation is None or recommendation.status != RecommendationStatus.ACTIVE:
            self._node_executions.mark_skipped(
                node_execution, reason=f"No active '{rule_name}' recommendation this run."
            )
            return "default", context

        if policy.effect == WorkflowPolicyEffect.SKIP:
            self._node_executions.mark_skipped(
                node_execution, reason=f"Policy '{policy.name}' says skip."
            )
            return "default", context

        if not self._policy_matches(policy, recommendation):
            self._node_executions.mark_skipped(
                node_execution,
                reason=f"Recommendation didn't match policy '{policy.name}' conditions.",
            )
            return "default", context

        workflow = self._workflows.get_by_id(execution.workflow_id)
        try:
            plan = self._plan_service.create_plan(
                recommendation.id,
                organization_id=execution.organization_id,
                user_id=workflow.created_by_user_id,
            )
        except VaultError as exc:
            self._node_executions.mark_failed(node_execution, error=str(exc))
            raise

        approval_request = self._approvals.get_by_plan(plan.id)
        if approval_request is None:
            self._node_executions.mark_failed(node_execution, error="Plan has no approval request.")
            raise ValidationError("Execution plan was created without an approval request.")

        # A policy may auto-run what only rearranges; anything that removes
        # files always waits for a person, whatever the policy says.
        if policy.effect == WorkflowPolicyEffect.AUTO_EXECUTE and not rule_removes_files(rule_name):
            try:
                self._approval_service.auto_decide_via_policy(approval_request.id, policy=policy)
            except VaultError as exc:
                self._node_executions.mark_failed(node_execution, error=str(exc))
                raise
            self._node_executions.mark_completed(
                node_execution,
                output_context={"execution_plan_id": str(plan.id), "auto_executed": True},
            )
            return "default", context

        # REQUIRE_APPROVAL — link this same ApprovalRequest to the node
        # execution too (it already has `execution_plan_id` set), so a
        # human's `decide()` call both runs the normal plan-approval flow
        # AND resumes this paused workflow (see ADR-021).
        approval_request.workflow_node_execution_id = node_execution.id
        self._db.flush()
        self._node_executions.mark_waiting_approval(node_execution)
        self._notify_recipients(
            node.config,
            organization_id=execution.organization_id,
            workflow_execution_id=execution.id,
            default_subject="Execution plan needs approval",
            default_body=(
                f"An execution plan for '{recommendation.title}' is waiting for your approval."
            ),
            context=context,
        )
        self._db.commit()
        return _WAITING, context

    def _handle_delay(
        self,
        execution: WorkflowExecution,
        node: WorkflowNode,
        node_execution: WorkflowNodeExecution,
        *,
        context: dict,
    ) -> tuple[str, dict]:
        seconds = int(node.config.get("seconds", 0))
        resume_at = datetime.now(UTC) + timedelta(seconds=seconds)
        self._node_executions.mark_waiting_delay(node_execution, resume_at=resume_at.isoformat())
        self._db.commit()
        self._enqueue_workflow_execution(execution.id, countdown=seconds)
        return _WAITING, context

    def _resume_waiting_delay(
        self, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        resume_at = datetime.fromisoformat(node_execution.output_context["resume_at"])
        if datetime.now(UTC) < resume_at:
            return _WAITING, context
        self._node_executions.mark_completed(
            node_execution, output_context={"delayed_until": resume_at.isoformat()}
        )
        return "default", context

    def _handle_notification(
        self,
        execution: WorkflowExecution,
        node: WorkflowNode,
        node_execution: WorkflowNodeExecution,
        *,
        context: dict,
    ) -> tuple[str, dict]:
        sent = self._notify_recipients(
            node.config,
            organization_id=execution.organization_id,
            workflow_execution_id=execution.id,
            default_subject="Workflow notification",
            default_body="",
            context=context,
        )
        self._node_executions.mark_completed(node_execution, output_context={"recipients": sent})
        return "default", context

    def _handle_end(
        self, node_execution: WorkflowNodeExecution, *, context: dict
    ) -> tuple[str, dict]:
        self._node_executions.mark_completed(node_execution, output_context={})
        return "default", context

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify_recipients(
        self,
        config: dict,
        *,
        organization_id: uuid.UUID,
        workflow_execution_id: uuid.UUID,
        default_subject: str,
        default_body: str,
        context: dict,
    ) -> int:
        channel = config.get("channel", "in_app")
        subject = self._render_template(config.get("subject", default_subject), context)
        body = self._render_template(config.get("body_template", default_body), context)
        recipients = self._resolve_recipients(
            config.get("recipients", []), organization_id=organization_id
        )
        for user in recipients:
            self._notifications.send(
                organization_id=organization_id,
                user_id=user.id,
                channel=channel,
                subject=subject,
                body=body,
                workflow_execution_id=workflow_execution_id,
            )
        return len(recipients)

    def _resolve_recipients(self, recipients: list[str], *, organization_id: uuid.UUID) -> list:
        role_names = [r for r in recipients if r in ("owner", "admin", "member")]
        explicit_ids = [r for r in recipients if r not in ("owner", "admin", "member")]

        resolved = []
        if role_names:
            resolved.extend(
                self._users.list_for_organization_by_roles(
                    organization_id=organization_id, role_names=role_names
                )
            )
        for raw_id in explicit_ids:
            user = self._users.get_by_id(uuid.UUID(raw_id))
            if user is not None:
                resolved.append(user)

        seen: set[uuid.UUID] = set()
        unique = []
        for user in resolved:
            if user.id not in seen:
                seen.add(user.id)
                unique.append(user)
        return unique

    @staticmethod
    def _policy_matches(policy: WorkflowPolicy, recommendation: Recommendation) -> bool:
        """Recommendation-level gating, not per-file inclusion/exclusion —
        a documented scope narrowing (see the Phase 9 completion report):
        `max_affected_files`/`min_confidence` on the aggregate
        recommendation, not a per-file folder/department exclusion list,
        which would need `ExecutionPlanService.create_plan` itself to
        accept a file-filtering parameter."""
        conditions = policy.conditions or {}
        max_affected_files = conditions.get("max_affected_files")
        if (
            max_affected_files is not None
            and len(recommendation.affected_file_ids) > max_affected_files
        ):
            return False
        min_confidence = conditions.get("min_confidence")
        return min_confidence is None or recommendation.confidence >= min_confidence

    @staticmethod
    def _evaluate_condition(expr: dict, context: dict) -> bool:
        if not expr:
            return True
        field = expr.get("field")
        op = expr.get("op", "eq")
        value = expr.get("value")
        actual = context.get(field)

        if op == "eq":
            return actual == value
        if op == "ne":
            return actual != value
        if op == "gt":
            return actual is not None and actual > value
        if op == "gte":
            return actual is not None and actual >= value
        if op == "lt":
            return actual is not None and actual < value
        if op == "lte":
            return actual is not None and actual <= value
        if op == "in":
            return actual in (value or [])
        if op == "contains":
            return value in (actual or [])
        raise ValidationError(f"Unknown condition operator: {op}")

    @staticmethod
    def _render_template(template: str, context: dict) -> str:
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace("{{" + key + "}}", str(value))
        return rendered

    def _check_cancelled(self, workflow_execution_id: uuid.UUID) -> None:
        if self._executions.is_cancel_requested(workflow_execution_id):
            raise WorkflowExecutionCancelled

    # ------------------------------------------------------------------
    # Celery re-enqueue (this app's own celery_app, never a cross-app
    # import — apps/backend injects its own producer functions into the
    # shared ApprovalService the same way; see that class's docstring)
    # ------------------------------------------------------------------

    @staticmethod
    def _enqueue_execution_job(execution_job_id: uuid.UUID) -> None:
        from worker.celery_app import celery_app

        celery_app.send_task("worker.execution.run", args=[str(execution_job_id)])

    @staticmethod
    def _enqueue_workflow_execution(
        workflow_execution_id: uuid.UUID, *, countdown: int = 0
    ) -> None:
        from worker.celery_app import celery_app

        celery_app.send_task(
            "worker.workflow.run", args=[str(workflow_execution_id)], countdown=countdown or None
        )
