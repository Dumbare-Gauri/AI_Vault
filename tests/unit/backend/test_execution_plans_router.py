import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import (
    get_execution_job_service,
    get_execution_plan_service,
)
from fastapi.testclient import TestClient
from vault_shared import ConflictError, NotFoundError, ValidationError

client = TestClient(app)


class _FakeExecutionStep:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.step_order = 0
        self.action_type = "remove_duplicate"
        self.target_file_id = uuid.uuid4()
        self.pre_state: dict = {}
        self.planned_change = {"action": "remove_duplicate"}
        self.status = "pending"


class _FakeExecutionPlan:
    def __init__(
        self,
        *,
        status: str = "pending_approval",
        risk_level: str = "medium",
        duplicate_group_id: uuid.UUID | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.organization_id = uuid.uuid4()
        self.recommendation_id = None if duplicate_group_id else uuid.uuid4()
        self.duplicate_group_id = duplicate_group_id
        self.status = status
        self.target_provider = "google_workspace"
        self.estimated_impact = "17 files, ~16.5 MB"
        self.estimated_storage_savings_bytes = 17_287_885
        self.risk_level = risk_level
        self.rollback_available = True
        self.required_permissions = ["google_workspace:drive:write"]
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class _FakeExecutionPlanDetail:
    def __init__(self, *, plan: _FakeExecutionPlan | None = None) -> None:
        self.plan = plan or _FakeExecutionPlan()
        self.steps = [_FakeExecutionStep()]


class _FakeExecutionJob:
    def __init__(self, *, status: str = "pending", is_rollback: bool = False) -> None:
        self.id = uuid.uuid4()
        self.execution_plan_id = uuid.uuid4()
        self.organization_id = uuid.uuid4()
        self.status = status
        self.is_rollback = is_rollback
        self.error = None
        self.started_at = None
        self.completed_at = None
        self.created_at = datetime.now(UTC)


@pytest.fixture
def fake_plan_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_execution_plan_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_execution_plan_service, None)


@pytest.fixture
def fake_job_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_execution_job_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_execution_job_service, None)


@pytest.fixture
def as_owner(owner_user):
    app.dependency_overrides[get_current_user] = lambda: owner_user
    yield owner_user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_create_execution_plan_requires_authentication(fake_plan_service) -> None:
    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )
    assert response.status_code == 401


def test_owner_can_create_an_execution_plan(as_owner, fake_plan_service) -> None:
    fake_plan_service.create_plan.return_value = _FakeExecutionPlan(status="approved")

    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 201
    assert response.json()["status"] == "approved"


def test_create_execution_plan_runs_immediately_with_no_approval_step(
    as_owner, fake_plan_service
) -> None:
    """ADR-026: no separate human-approval step exists for a
    directly-triggered plan — `require_approval=False` is passed straight
    through to the service, which approves and enqueues it in one call."""
    recommendation_id = uuid.uuid4()
    fake_plan_service.create_plan.return_value = _FakeExecutionPlan(status="approved")

    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(recommendation_id)}
    )

    assert response.status_code == 201
    fake_plan_service.create_plan.assert_called_once_with(
        recommendation_id,
        organization_id=as_owner.organization_id,
        user_id=as_owner.id,
        require_approval=False,
    )


def test_create_execution_plan_fails_the_request_when_the_service_cannot_start_it(
    as_owner, fake_plan_service
) -> None:
    """There is no more silent 'left pending for later review' outcome
    (ADR-026) — if the plan cannot be started (e.g. the connector lacks
    write scope), creating it fails outright."""
    fake_plan_service.create_plan.side_effect = ValidationError(
        "Cannot start — execution permissions are not satisfied."
    )

    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 422


def test_member_cannot_create_an_execution_plan(as_member, fake_plan_service) -> None:
    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 403
    fake_plan_service.create_plan.assert_not_called()


def test_owner_can_create_an_execution_plan_from_a_duplicate_group(
    as_owner, fake_plan_service
) -> None:
    group_id = uuid.uuid4()
    fake_plan_service.create_plan_from_duplicate_group.return_value = _FakeExecutionPlan(
        duplicate_group_id=group_id
    )

    response = client.post(
        "/v1/execution-plans", json={"duplicate_group_id": str(group_id)}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["duplicate_group_id"] == str(group_id)
    assert body["recommendation_id"] is None
    fake_plan_service.create_plan_from_duplicate_group.assert_called_once_with(
        group_id,
        organization_id=as_owner.organization_id,
        user_id=as_owner.id,
        require_approval=False,
    )
    fake_plan_service.create_plan.assert_not_called()


def test_create_execution_plan_rejects_neither_origin_provided(as_owner, fake_plan_service) -> None:
    response = client.post("/v1/execution-plans", json={})

    assert response.status_code == 422
    fake_plan_service.create_plan.assert_not_called()
    fake_plan_service.create_plan_from_duplicate_group.assert_not_called()


def test_create_execution_plan_rejects_both_origins_provided(as_owner, fake_plan_service) -> None:
    response = client.post(
        "/v1/execution-plans",
        json={"recommendation_id": str(uuid.uuid4()), "duplicate_group_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    fake_plan_service.create_plan.assert_not_called()
    fake_plan_service.create_plan_from_duplicate_group.assert_not_called()


def test_create_execution_plan_propagates_validation_error_for_a_non_executable_rule(
    as_owner, fake_plan_service
) -> None:
    fake_plan_service.create_plan.side_effect = ValidationError(
        "Recommendations from rule 'orphaned_ownership' have no supported execution action yet."
    )

    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 422


def test_create_execution_plan_propagates_conflict_for_a_duplicate_plan(
    as_owner, fake_plan_service
) -> None:
    fake_plan_service.create_plan.side_effect = ConflictError(
        "An execution plan is already pending or in progress for this recommendation."
    )

    response = client.post(
        "/v1/execution-plans", json={"recommendation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 409


def test_create_permanent_delete_plan_requires_authentication(fake_plan_service) -> None:
    response = client.post("/v1/execution-plans/permanent-delete", json={"file_ids": [str(uuid.uuid4())]})
    assert response.status_code == 401


def test_owner_can_create_a_permanent_delete_plan(as_owner, fake_plan_service) -> None:
    plan = _FakeExecutionPlan()
    plan.rollback_available = False
    fake_plan_service.create_permanent_delete_plan.return_value = plan

    response = client.post(
        "/v1/execution-plans/permanent-delete", json={"file_ids": [str(uuid.uuid4())]}
    )

    assert response.status_code == 201
    assert response.json()["rollback_available"] is False


def test_create_permanent_delete_plan_runs_immediately_with_no_approval_step(
    as_owner, fake_plan_service
) -> None:
    """ADR-026: permanent delete runs immediately like every other plan —
    the frontend's own type-to-confirm dialog is the only gate before this
    endpoint is ever called; there is no server-side approval step."""
    file_id = uuid.uuid4()
    fake_plan_service.create_permanent_delete_plan.return_value = _FakeExecutionPlan()

    response = client.post(
        "/v1/execution-plans/permanent-delete", json={"file_ids": [str(file_id)]}
    )

    assert response.status_code == 201
    fake_plan_service.create_permanent_delete_plan.assert_called_once_with(
        [file_id],
        organization_id=as_owner.organization_id,
        user_id=as_owner.id,
        require_approval=False,
    )


def test_member_cannot_create_a_permanent_delete_plan(as_member, fake_plan_service) -> None:
    response = client.post(
        "/v1/execution-plans/permanent-delete", json={"file_ids": [str(uuid.uuid4())]}
    )

    assert response.status_code == 403
    fake_plan_service.create_permanent_delete_plan.assert_not_called()


def test_create_permanent_delete_plan_rejects_an_empty_selection(as_owner, fake_plan_service) -> None:
    response = client.post("/v1/execution-plans/permanent-delete", json={"file_ids": []})

    assert response.status_code == 422
    fake_plan_service.create_permanent_delete_plan.assert_not_called()


def test_create_permanent_delete_plan_propagates_validation_error(
    as_owner, fake_plan_service
) -> None:
    fake_plan_service.create_permanent_delete_plan.side_effect = ValidationError(
        "None of the selected files are eligible."
    )

    response = client.post(
        "/v1/execution-plans/permanent-delete", json={"file_ids": [str(uuid.uuid4())]}
    )

    assert response.status_code == 422


def test_list_execution_plans_returns_items(as_member, fake_plan_service) -> None:
    fake_plan_service.list_for_organization.return_value = [_FakeExecutionPlan()]

    response = client.get("/v1/execution-plans")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_execution_plan_returns_detail_with_steps(as_member, fake_plan_service) -> None:
    fake_plan_service.get_detail.return_value = _FakeExecutionPlanDetail()

    response = client.get(f"/v1/execution-plans/{uuid.uuid4()}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["steps"]) == 1
    assert body["steps"][0]["action_type"] == "remove_duplicate"


def test_get_execution_plan_returns_not_found_for_a_missing_plan(
    as_member, fake_plan_service
) -> None:
    fake_plan_service.get_detail.side_effect = NotFoundError("Execution plan not found.")

    response = client.get(f"/v1/execution-plans/{uuid.uuid4()}")

    assert response.status_code == 404


def test_owner_can_trigger_a_rollback(as_owner, fake_job_service) -> None:
    fake_job_service.trigger_rollback.return_value = _FakeExecutionJob(is_rollback=True)

    response = client.post(f"/v1/execution-plans/{uuid.uuid4()}/rollback")

    assert response.status_code == 201
    assert response.json()["is_rollback"] is True


def test_member_cannot_trigger_a_rollback(as_member, fake_job_service) -> None:
    response = client.post(f"/v1/execution-plans/{uuid.uuid4()}/rollback")

    assert response.status_code == 403
    fake_job_service.trigger_rollback.assert_not_called()


def test_rollback_propagates_conflict_when_nothing_to_roll_back(
    as_owner, fake_job_service
) -> None:
    fake_job_service.trigger_rollback.side_effect = ConflictError(
        "Nothing to roll back for this plan."
    )

    response = client.post(f"/v1/execution-plans/{uuid.uuid4()}/rollback")

    assert response.status_code == 409
