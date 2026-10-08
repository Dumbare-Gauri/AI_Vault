from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.application.dashboard_service import DashboardOverview, IntelligenceSummary
from app.infrastructure.cache.redis_client import get_redis
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_dashboard_service
from fastapi.testclient import TestClient

client = TestClient(app)

_INTELLIGENCE = IntelligenceSummary(
    files_analyzed=40,
    entities_by_type={"project": 3},
    lifecycle_by_state={"archive_candidate": 5},
    organize_suggestions=2,
)


class _FakeSnapshot:
    def __init__(self) -> None:
        self.connected_providers = 1
        self.total_files = 100
        self.total_folders = 10
        self.total_storage_bytes = 1024
        self.classified_files = 90
        self.unclassified_files = 10
        self.pending_enrichment_files = 5
        self.embedded_files = 80
        self.relationship_count = 20
        self.active_recommendations = 3
        self.knowledge_completeness_score = 0.9
        self.created_at = datetime.now(UTC)


@pytest.fixture
def fake_dashboard_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_dashboard_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_dashboard_service, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_get_dashboard_requires_authentication(fake_dashboard_service) -> None:
    response = client.get("/v1/dashboard")
    assert response.status_code == 401


def test_get_dashboard_returns_the_overview(as_member, fake_dashboard_service) -> None:
    fake_dashboard_service.get_overview.return_value = DashboardOverview(
        connectors=[],
        latest_snapshot=_FakeSnapshot(),
        snapshot_history=[_FakeSnapshot()],
        recent_insights=[],
        recent_activity=[],
        latest_scan_status="completed",
        latest_enrichment_status="completed",
        latest_embedding_status="completed",
        latest_recommendation_status="completed",
        intelligence=_INTELLIGENCE,
    )

    response = client.get("/v1/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_snapshot"]["total_files"] == 100
    assert body["latest_scan_status"] == "completed"
    assert body["intelligence"]["entities_by_type"] == {"project": 3}
    fake_dashboard_service.get_overview.assert_called_once_with(as_member.organization_id)


def test_get_dashboard_serves_the_second_request_from_cache(
    as_member, fake_dashboard_service
) -> None:
    # Phase 10 (ADR-022) — a real Redis round-trip, not a mocked one: this
    # asserts the *service* (the DB-hitting path) is called exactly once
    # across two requests, which is only true if the second request was
    # actually served from the real cache the first request wrote to.
    get_redis().delete(f"dashboard:{as_member.organization_id}")
    fake_dashboard_service.get_overview.return_value = DashboardOverview(
        connectors=[],
        latest_snapshot=_FakeSnapshot(),
        snapshot_history=[],
        recent_insights=[],
        recent_activity=[],
        latest_scan_status="completed",
        latest_enrichment_status="completed",
        latest_embedding_status="completed",
        latest_recommendation_status="completed",
        intelligence=_INTELLIGENCE,
    )

    first = client.get("/v1/dashboard")
    second = client.get("/v1/dashboard")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    fake_dashboard_service.get_overview.assert_called_once_with(as_member.organization_id)

    get_redis().delete(f"dashboard:{as_member.organization_id}")


def test_get_dashboard_handles_no_snapshot_yet(as_member, fake_dashboard_service) -> None:
    fake_dashboard_service.get_overview.return_value = DashboardOverview(
        connectors=[],
        latest_snapshot=None,
        snapshot_history=[],
        recent_insights=[],
        recent_activity=[],
        latest_scan_status=None,
        latest_enrichment_status=None,
        latest_embedding_status=None,
        latest_recommendation_status=None,
        intelligence=_INTELLIGENCE,
    )

    response = client.get("/v1/dashboard")

    assert response.status_code == 200
    assert response.json()["latest_snapshot"] is None
