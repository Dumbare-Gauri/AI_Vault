import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import (
    get_organization_entity_service,
    get_organization_recommendation_service,
)
from vault_shared import ConflictError

client = TestClient(app)


class _FakeEntity:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.entity_type = "project"
        self.name = "Acme Rebrand"
        self.confidence = 0.86
        self.evidence = [{"type": "ai_inference", "description": "Shared file names."}]
        self.status = "active"
        self.created_at = now
        self.updated_at = now


class _FakeRecommendation:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.kind = "group_project"
        self.entity_id = uuid.uuid4()
        self.title = "Consolidate 'Acme Rebrand' into one folder"
        self.reasoning_summary = "Spread across 2 folders."
        self.evidence = [{"type": "scattered_locations", "description": "Found in 2 folders."}]
        self.confidence = 0.86
        self.affected_file_ids = [str(uuid.uuid4())]
        self.current_locations = [{"folder_id": str(uuid.uuid4()), "path": "/A", "file_count": 1}]
        self.suggested_destination = ["Projects", "Acme Rebrand"]
        self.estimated_storage_impact_bytes = 100
        self.status = "active"
        self.execution_plan_id = None
        self.created_at = now
        self.updated_at = now


@pytest.fixture
def entity_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_organization_entity_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_organization_entity_service, None)


@pytest.fixture
def recommendation_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_organization_recommendation_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_organization_recommendation_service, None)


@pytest.fixture
def as_user(request):
    user = request.getfixturevalue(request.param)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("as_user", ["member_user"], indirect=True)
def test_listing_entities_is_not_shadowed_by_another_route(as_user, entity_service) -> None:
    """Regression: under `/intelligence/entities` this was swallowed by
    `GET /intelligence/{intelligence_job_id}` and returned 422."""
    entity_service.list_for_organization.return_value = [_FakeEntity()]

    response = client.get("/v1/organization/entities")

    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Acme Rebrand"


@pytest.mark.parametrize("as_user", ["member_user"], indirect=True)
def test_recommendation_detail_includes_the_evidence(as_user, recommendation_service) -> None:
    recommendation = _FakeRecommendation()
    recommendation_service.get_owned.return_value = recommendation

    response = client.get(f"/v1/organization-recommendations/{recommendation.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["evidence"][0]["type"] == "scattered_locations"
    assert body["suggested_destination"] == ["Projects", "Acme Rebrand"]


@pytest.mark.parametrize("as_user", ["member_user"], indirect=True)
def test_a_member_cannot_apply_a_recommendation(as_user, recommendation_service) -> None:
    response = client.post(f"/v1/organization-recommendations/{uuid.uuid4()}/apply")

    assert response.status_code == 403
    recommendation_service.trigger_apply.assert_not_called()


@pytest.mark.parametrize("as_user", ["member_user"], indirect=True)
def test_a_member_cannot_start_an_analysis(as_user, recommendation_service) -> None:
    response = client.post("/v1/organization/analyze")

    assert response.status_code == 403


@pytest.mark.parametrize("as_user", ["owner_user"], indirect=True)
def test_an_owner_can_apply_a_recommendation(as_user, recommendation_service) -> None:
    recommendation = _FakeRecommendation()
    recommendation_service.trigger_apply.return_value = recommendation

    response = client.post(f"/v1/organization-recommendations/{recommendation.id}/apply")

    assert response.status_code == 200
    assert recommendation_service.trigger_apply.call_args.kwargs["user_id"] == as_user.id


@pytest.mark.parametrize("as_user", ["owner_user"], indirect=True)
def test_a_second_concurrent_analysis_is_a_conflict(as_user, recommendation_service) -> None:
    recommendation_service.trigger_analysis.side_effect = ConflictError("already running")

    response = client.post("/v1/organization/analyze")

    assert response.status_code == 409
