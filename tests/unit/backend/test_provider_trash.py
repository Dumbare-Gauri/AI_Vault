import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.application import storage_operation_service as service_module
from app.application.storage_operation_service import StorageOperationService
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_storage_operation_service
from vault_shared import NotFoundError, ValidationError

client = TestClient(app)

_SUMMARY = {
    "ok": True,
    "file_count": 2,
    "total_bytes": 2048,
    "not_backed_up_count": 1,
    "largest": [{"name": "Old.zip", "size_bytes": 2000, "backed_up": False}],
}


def _service_owning_the_connector() -> StorageOperationService:
    db = MagicMock()
    service = StorageOperationService(db)
    service._check_owned = MagicMock()  # type: ignore[method-assign]
    return service


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(service_module, "get_redis", lambda: fake)
    return fake


def _start(service: StorageOperationService, connector_id: uuid.UUID, *, confirmed: bool) -> str:
    return service.start_empty_trash(
        connector_id,
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        expected_count=2,
        confirmed=confirmed,
    )


def test_without_the_acknowledgement_nothing_reaches_the_provider(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()

    with patch.object(service_module, "start_storage_operation") as start:
        with pytest.raises(ValidationError):
            _start(service, uuid.uuid4(), confirmed=False)

    start.assert_not_called()


def test_the_acknowledged_reviewed_count_is_sent_to_the_worker(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()
    connector_id = uuid.uuid4()

    with patch.object(service_module, "start_storage_operation", return_value="op-1") as start:
        operation_id = _start(service, connector_id, confirmed=True)

    assert operation_id == "op-1"
    assert start.call_args.kwargs["args"][1:] == [str(connector_id), 2]


def test_a_repeated_submit_joins_the_run_already_in_progress(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()
    connector_id = uuid.uuid4()

    with patch.object(service_module, "start_storage_operation", return_value="op-1") as start:
        first = _start(service, connector_id, confirmed=True)
        second = _start(service, connector_id, confirmed=True)

    assert first == second
    start.assert_called_once()


def test_a_running_operation_reports_running(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()

    with patch.object(service_module, "storage_operation_result", return_value=None):
        status = service.empty_trash_status(uuid.uuid4(), "op-1", organization_id=uuid.uuid4())

    assert status == {"status": "running"}


def test_a_finished_operation_reports_what_was_not_deleted(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()
    connector_id = uuid.uuid4()
    outcome = {
        **_SUMMARY,
        "connector_id": str(connector_id),
        "not_deleted": [{"name": "Locked.db", "size_bytes": 5, "backed_up": False}],
    }

    with patch.object(service_module, "storage_operation_result", return_value=outcome):
        status = service.empty_trash_status(connector_id, "op-1", organization_id=uuid.uuid4())

    assert status["summary"]["not_deleted"][0]["name"] == "Locked.db"


def test_another_connections_operation_cannot_be_read(redis: _FakeRedis) -> None:
    service = _service_owning_the_connector()
    outcome = {**_SUMMARY, "connector_id": str(uuid.uuid4())}

    with patch.object(service_module, "storage_operation_result", return_value=outcome):
        with pytest.raises(NotFoundError):
            service.empty_trash_status(uuid.uuid4(), "op-1", organization_id=uuid.uuid4())


def test_a_member_cannot_empty_the_trash(member_user) -> None:
    fake_service = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_storage_operation_service] = lambda: fake_service
    try:
        response = client.post(
            f"/v1/connectors/{uuid.uuid4()}/provider-trash/empty",
            json={"expected_count": 1, "confirmed": True},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_storage_operation_service, None)

    assert response.status_code == 403
    fake_service.start_empty_trash.assert_not_called()


def test_the_preview_reports_what_would_be_deleted(owner_user) -> None:
    fake_service = MagicMock()
    fake_service.preview_trash.return_value = _SUMMARY
    app.dependency_overrides[get_current_user] = lambda: owner_user
    app.dependency_overrides[get_storage_operation_service] = lambda: fake_service
    try:
        response = client.get(f"/v1/connectors/{uuid.uuid4()}/provider-trash")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_storage_operation_service, None)

    assert response.status_code == 200
    assert response.json()["not_backed_up_count"] == 1
