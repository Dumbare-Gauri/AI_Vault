import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_storage_intelligence_service
from fastapi.testclient import TestClient
from vault_shared import ConflictError, NotFoundError

client = TestClient(app)


class _FakeStorageAnalysisJob:
    def __init__(self, *, organization_id: uuid.UUID | None = None) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id or uuid.uuid4()
        self.triggered_by = "manual"
        self.status = "pending"
        self.error = None
        self.started_at = None
        self.completed_at = None
        self.created_at = datetime.now(UTC)


class _FakeDuplicateGroup:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.checksum = "abc123"
        self.file_count = 2
        self.total_size_bytes = 2000
        self.recoverable_size_bytes = 1000
        self.recommended_keep_file_id = uuid.uuid4()
        self.recommended_keep_reason = "Most recently modified copy."
        self.recommended_keep_confidence = 0.86
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


@pytest.fixture
def fake_storage_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_storage_intelligence_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_storage_intelligence_service, None)


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


def test_overview_requires_authentication(fake_storage_service) -> None:
    response = client.get("/v1/storage/overview")
    assert response.status_code == 401


def test_overview_returns_nulls_when_no_analysis_has_run(as_member, fake_storage_service) -> None:
    fake_storage_service.get_latest_snapshot.return_value = None
    fake_storage_service.get_latest_job.return_value = None

    response = client.get("/v1/storage/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["total_size_bytes"] is None
    assert body["last_analyzed_at"] is None


def test_owner_can_trigger_analysis(as_owner, fake_storage_service) -> None:
    job = _FakeStorageAnalysisJob(organization_id=as_owner.organization_id)
    fake_storage_service.trigger_analysis.return_value = job

    response = client.post("/v1/storage/analyze")

    assert response.status_code == 201
    assert response.json()["id"] == str(job.id)


def test_member_cannot_trigger_analysis(as_member, fake_storage_service) -> None:
    response = client.post("/v1/storage/analyze")
    assert response.status_code == 403
    fake_storage_service.trigger_analysis.assert_not_called()


def test_trigger_analysis_propagates_conflict_when_already_running(
    as_owner, fake_storage_service
) -> None:
    fake_storage_service.trigger_analysis.side_effect = ConflictError(
        "A storage analysis run is already pending or running for this organization."
    )

    response = client.post("/v1/storage/analyze")

    assert response.status_code == 409


def test_list_duplicates_passes_pagination_params(as_member, fake_storage_service) -> None:
    fake_storage_service.list_duplicate_groups.return_value = ([], 0)

    response = client.get("/v1/storage/duplicates?limit=10&offset=20")

    assert response.status_code == 200
    fake_storage_service.list_duplicate_groups.assert_called_once_with(
        as_member.organization_id, limit=10, offset=20, connector_id=None
    )


def test_get_duplicate_group_returns_not_found_for_another_organizations_group(
    as_member, fake_storage_service
) -> None:
    fake_storage_service.get_duplicate_group.side_effect = NotFoundError(
        "Duplicate group not found."
    )

    response = client.get(f"/v1/storage/duplicates/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_duplicate_group_includes_members(as_member, fake_storage_service) -> None:
    group = _FakeDuplicateGroup()

    class _FakeFile:
        def __init__(self) -> None:
            self.id = group.recommended_keep_file_id
            self.name = "report.pdf"
            self.path = "/report.pdf"
            self.mime_type = "application/pdf"
            self.size_bytes = 1000
            self.provider_modified_at = None
            self.provider_viewed_at = None
            self.storage_source_id = uuid.uuid4()

    fake_storage_service.get_duplicate_group.return_value = (group, [(_FakeFile(), True)])

    response = client.get(f"/v1/storage/duplicates/{group.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["file_count"] == 2
    assert len(body["members"]) == 1
    assert body["members"][0]["is_recommended_keep"] is True


def test_large_files_requires_authentication(fake_storage_service) -> None:
    response = client.get("/v1/storage/large-files")
    assert response.status_code == 401


def test_list_candidates_returns_empty_list_when_none_found(
    as_member, fake_storage_service
) -> None:
    fake_storage_service.list_candidates.return_value = ([], 0)

    response = client.get("/v1/storage/candidates")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}
