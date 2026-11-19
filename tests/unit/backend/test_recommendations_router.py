import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_recommendation_service
from fastapi.testclient import TestClient
from vault_shared import ConflictError, ForbiddenError, NotFoundError
from conftest import WORKSPACE

client = TestClient(app)


class _FakeRecommendation:
    def __init__(self, *, category: str = "storage_optimization") -> None:
        self.id = uuid.uuid4()
        self.category = category
        self.rule_name = "duplicate_files"
        self.title = "12 duplicate file groups found"
        self.description = "Some duplicates were found."
        self.confidence = 0.9
        self.estimated_impact = "~2.1 GB could be reclaimed"
        self.impact_value = 2_100_000_000.0
        self.risk_level = "low"
        self.suggested_action = "Review and remove redundant copies."
        self.requires_approval = True
        self.related_departments: list[str] = []
        self.affected_file_ids: list[str] = [str(uuid.uuid4())]
        self.status = "active"
        self.priority_score = 54.0
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.resolved_at = None


class _FakeRecommendationJob:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.organization_id = uuid.uuid4()
        self.triggered_by = "manual"
        self.status = "pending"
        self.error = None
        self.recommendations_active = None
        self.started_at = None
        self.completed_at = None
        self.created_at = datetime.now(UTC)


@pytest.fixture
def fake_recommendation_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_recommendation_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_recommendation_service, None)


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


def test_list_recommendations_requires_authentication(fake_recommendation_service) -> None:
    response = client.get("/v1/recommendations")
    assert response.status_code == 401


def test_list_recommendations_returns_items(as_member, fake_recommendation_service) -> None:
    fake_recommendation_service.list_for_organization.return_value = [_FakeRecommendation()]

    response = client.get("/v1/recommendations")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    fake_recommendation_service.list_for_organization.assert_called_once_with(
        as_member.organization_id,
        role=as_member.role.name,
        category=None,
        status="active",
        search=None,
        connector_id=WORKSPACE.id,
    )


def test_list_recommendations_passes_filters_through(
    as_member, fake_recommendation_service
) -> None:
    fake_recommendation_service.list_for_organization.return_value = []

    response = client.get("/v1/recommendations?category=security&status=resolved&search=invoice")

    assert response.status_code == 200
    fake_recommendation_service.list_for_organization.assert_called_once_with(
        as_member.organization_id,
        role=as_member.role.name,
        category="security",
        status="resolved",
        search="invoice",
        connector_id=WORKSPACE.id,
    )


def test_get_recommendation_returns_detail(as_member, fake_recommendation_service) -> None:
    fake_recommendation_service.get_owned.return_value = _FakeRecommendation()

    response = client.get(f"/v1/recommendations/{uuid.uuid4()}")

    assert response.status_code == 200
    assert response.json()["rule_name"] == "duplicate_files"


def test_get_recommendation_returns_not_found_for_a_missing_recommendation(
    as_member, fake_recommendation_service
) -> None:
    fake_recommendation_service.get_owned.side_effect = NotFoundError("Recommendation not found.")

    response = client.get(f"/v1/recommendations/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_recommendation_returns_forbidden_for_a_hidden_security_item(
    as_member, fake_recommendation_service
) -> None:
    fake_recommendation_service.get_owned.side_effect = ForbiddenError(
        "You do not have permission to view this recommendation."
    )

    response = client.get(f"/v1/recommendations/{uuid.uuid4()}")

    assert response.status_code == 403


def test_owner_can_trigger_a_refresh(as_owner, fake_recommendation_service) -> None:
    fake_recommendation_service.trigger_refresh.return_value = _FakeRecommendationJob()

    response = client.post("/v1/recommendations/refresh")

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_member_cannot_trigger_a_refresh(as_member, fake_recommendation_service) -> None:
    response = client.post("/v1/recommendations/refresh")

    assert response.status_code == 403
    fake_recommendation_service.trigger_refresh.assert_not_called()


def test_refresh_propagates_conflict_when_already_running(
    as_owner, fake_recommendation_service
) -> None:
    fake_recommendation_service.trigger_refresh.side_effect = ConflictError(
        "A recommendation run is already pending or running for this organization."
    )

    response = client.post("/v1/recommendations/refresh")

    assert response.status_code == 409
