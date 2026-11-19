import uuid

from sqlalchemy.orm import Session

from vault_shared.db.models import ExecutionPlan, ExecutionPlanStatus


class ExecutionPlanRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        recommendation_id: uuid.UUID | None = None,
        duplicate_group_id: uuid.UUID | None = None,
        created_by_user_id: uuid.UUID,
        target_provider: str,
        estimated_impact: str,
        estimated_storage_savings_bytes: int | None,
        risk_level: str,
        rollback_available: bool,
        required_permissions: list[str],
    ) -> ExecutionPlan:
        plan = ExecutionPlan(
            organization_id=organization_id,
            recommendation_id=recommendation_id,
            duplicate_group_id=duplicate_group_id,
            created_by_user_id=created_by_user_id,
            target_provider=target_provider,
            estimated_impact=estimated_impact,
            estimated_storage_savings_bytes=estimated_storage_savings_bytes,
            risk_level=risk_level,
            rollback_available=rollback_available,
            required_permissions=required_permissions,
        )
        self._session.add(plan)
        self._session.flush()
        return plan

    def get_by_id(self, execution_plan_id: uuid.UUID) -> ExecutionPlan | None:
        return self._session.get(ExecutionPlan, execution_plan_id)

    def get_owned(
        self, execution_plan_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> ExecutionPlan | None:
        return (
            self._session.query(ExecutionPlan)
            .filter_by(id=execution_plan_id, organization_id=organization_id)
            .first()
        )

    def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        provider: str | None = None,
    ) -> list[ExecutionPlan]:
        """With `provider`, only actions on that storage — an organization
        has at most one connection per provider."""
        query = self._session.query(ExecutionPlan).filter_by(organization_id=organization_id)
        if provider is not None:
            query = query.filter(ExecutionPlan.target_provider == provider)
        if status is not None:
            query = query.filter(ExecutionPlan.status == status)
        return (
            query.order_by(ExecutionPlan.created_at.desc()).limit(limit).offset(offset).all()
        )

    def has_active_plan_for_recommendation(self, recommendation_id: uuid.UUID) -> bool:
        """Phase 8 spec's "Prevention of duplicate execution" — a
        recommendation can only have one plan in flight at a time (not yet
        rejected/expired/completed/rolled back)."""
        active_statuses = [
            ExecutionPlanStatus.PENDING_APPROVAL,
            ExecutionPlanStatus.APPROVED,
            ExecutionPlanStatus.EXECUTING,
        ]
        return (
            self._session.query(ExecutionPlan)
            .filter(
                ExecutionPlan.recommendation_id == recommendation_id,
                ExecutionPlan.status.in_(active_statuses),
            )
            .first()
            is not None
        )

    def has_active_plan_for_duplicate_group(self, duplicate_group_id: uuid.UUID) -> bool:
        active_statuses = [
            ExecutionPlanStatus.PENDING_APPROVAL,
            ExecutionPlanStatus.APPROVED,
            ExecutionPlanStatus.EXECUTING,
        ]
        return (
            self._session.query(ExecutionPlan)
            .filter(
                ExecutionPlan.duplicate_group_id == duplicate_group_id,
                ExecutionPlan.status.in_(active_statuses),
            )
            .first()
            is not None
        )

    def update_status(self, plan: ExecutionPlan, *, status: str) -> None:
        plan.status = status
        self._session.flush()
