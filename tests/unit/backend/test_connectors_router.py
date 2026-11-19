import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_connector_service
from fastapi.testclient import TestClient
from vault_shared import ConflictError, NotFoundError
from vault_shared.db.models import ConnectorStatus

client = TestClient(app)


class _FakeConnector:
    def __init__(self, *, organization_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.provider = "google_workspace"
        self.status = ConnectorStatus.CONNECTED
        self.account_email = "founder@acme.com"
        self.workspace_domain = "acme.com"
        self.last_verified_at = datetime.now(UTC)
        self.last_failed_at = None
        self.last_error = None
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.display_name = None
        self.last_synced_at = None
        self.storage_used_bytes = 5_000
        self.storage_total_bytes = 15_000
        self.storage_trash_bytes = 0
        self.quota_checked_at = None


@pytest.fixture
def fake_connector_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_connector_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_connector_service, None)


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


def test_list_requires_authentication(fake_connector_service) -> None:
    response = client.get("/v1/connectors")
    assert response.status_code == 401


def test_list_returns_connectors_for_the_users_organization(as_member, fake_connector_service) -> None:
    fake_connector_service.list_for_organization.return_value = [
        _FakeConnector(organization_id=as_member.organization_id)
    ]

    response = client.get("/v1/connectors")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["account_email"] == "founder@acme.com"
    fake_connector_service.list_for_organization.assert_called_once_with(as_member.organization_id)


def test_owner_can_initiate_connect(as_owner, fake_connector_service) -> None:
    fake_connector_service.initiate_connect.return_value = "https://accounts.google.com/o/oauth2/v2/auth?..."

    response = client.post("/v1/connectors/google/connect")

    assert response.status_code == 200
    assert response.json()["authorize_url"].startswith("https://accounts.google.com")


def test_member_cannot_initiate_connect(as_member, fake_connector_service) -> None:
    response = client.post("/v1/connectors/google/connect")

    assert response.status_code == 403
    fake_connector_service.initiate_connect.assert_not_called()


def test_initiate_connect_propagates_conflict_when_already_connected(as_owner, fake_connector_service) -> None:
    fake_connector_service.initiate_connect.side_effect = ConflictError(
        "Google Workspace is already connected for this organization."
    )

    response = client.post("/v1/connectors/google/connect")

    assert response.status_code == 409


def test_owner_can_complete_the_callback(as_owner, fake_connector_service) -> None:
    fake_connector_service.complete_connect.return_value = _FakeConnector(
        organization_id=as_owner.organization_id
    )

    response = client.post(
        "/v1/connectors/google/callback", json={"code": "auth-code", "state": "state-abc"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    fake_connector_service.complete_connect.assert_called_once()
    assert fake_connector_service.complete_connect.call_args.kwargs["code"] == "auth-code"
    assert fake_connector_service.complete_connect.call_args.kwargs["state"] == "state-abc"


def test_member_cannot_complete_the_callback(as_member, fake_connector_service) -> None:
    response = client.post(
        "/v1/connectors/google/callback", json={"code": "auth-code", "state": "state-abc"}
    )

    assert response.status_code == 403


def test_callback_requires_code_and_state(as_owner, fake_connector_service) -> None:
    response = client.post("/v1/connectors/google/callback", json={})
    assert response.status_code == 422


def test_status_returns_not_found_for_a_connector_in_another_organization(
    as_member, fake_connector_service
) -> None:
    fake_connector_service.get_owned.side_effect = NotFoundError("Connector not found.")

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/status")

    assert response.status_code == 404


def test_any_authenticated_member_can_verify(as_member, fake_connector_service) -> None:
    connector = _FakeConnector(organization_id=as_member.organization_id)
    fake_connector_service.get_owned.return_value = connector
    fake_connector_service.verify.return_value = connector

    response = client.post(f"/v1/connectors/{connector.id}/verify")

    assert response.status_code == 200
    fake_connector_service.verify.assert_called_once()


def test_member_cannot_disconnect(as_member, fake_connector_service) -> None:
    response = client.post(f"/v1/connectors/{uuid.uuid4()}/disconnect")

    assert response.status_code == 403
    fake_connector_service.disconnect.assert_not_called()


def test_owner_can_disconnect(as_owner, fake_connector_service) -> None:
    connector = _FakeConnector(organization_id=as_owner.organization_id)
    connector.status = ConnectorStatus.DISCONNECTED
    fake_connector_service.get_owned.return_value = connector
    fake_connector_service.disconnect.return_value = connector

    response = client.post(f"/v1/connectors/{connector.id}/disconnect")

    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
