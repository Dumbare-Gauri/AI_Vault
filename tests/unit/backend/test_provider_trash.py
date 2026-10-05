import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.application import storage_operation_service as service_module
from app.application.storage_operation_service import StorageOperationService
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_storage_operation_service
from vault_shared import ValidationError

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


def test_a_wrong_confirmation_never_reaches_the_provider() -> None:
    service = _service_owning_the_connector()

    with patch.object(service_module, "run_storage_operation") as run:
        with pytest.raises(ValidationError):
            service.empty_trash(
                uuid.uuid4(),
                organization_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                expected_count=2,
                confirmation="yes",
            )

    run.assert_not_called()


def test_the_typed_confirmation_and_reviewed_count_are_sent_to_the_worker() -> None:
    service = _service_owning_the_connector()
    connector_id = uuid.uuid4()

    with patch.object(service_module, "run_storage_operation", return_value=_SUMMARY) as run:
        service.empty_trash(
            connector_id,
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            expected_count=2,
            confirmation="empty trash",
        )

    assert run.call_args.kwargs["args"][1:] == [str(connector_id), 2]


def test_a_member_cannot_empty_the_trash(member_user) -> None:
    fake_service = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: member_user
    app.dependency_overrides[get_storage_operation_service] = lambda: fake_service
    try:
        response = client.post(
            f"/v1/connectors/{uuid.uuid4()}/provider-trash/empty",
            json={"expected_count": 1, "confirmation": "EMPTY TRASH"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_storage_operation_service, None)

    assert response.status_code == 403
    fake_service.empty_trash.assert_not_called()


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


def test_spacing_in_the_confirmation_does_not_matter() -> None:
    service = _service_owning_the_connector()

    with patch.object(service_module, "run_storage_operation", return_value=_SUMMARY) as run:
        service.empty_trash(
            uuid.uuid4(),
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            expected_count=2,
            confirmation=" Empty  trash ",
        )

    run.assert_called_once()
