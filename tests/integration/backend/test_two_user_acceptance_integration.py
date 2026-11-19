"""Two people use AI Vault side by side, through the real HTTP API and the
real database: each signs in with their own Google account, connects their
own Google Drive and their own computer, and works only with their own
files, jobs, tokens and agent. Deleting one account leaves the other
untouched.

Only Google is replaced — the ID-token check, the OAuth code exchange,
token refresh and revocation — by fakes that hand each person distinct
credentials. Not covered here, and needing two real Google accounts and a
configured OAuth client: Google's own sign-in and ID-token signature, the
consent screen and the scopes it actually grants, a real Drive listing per
account, refresh after a real expiry, and Google honouring the revoke.
"""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.infrastructure.auth.google_identity import GoogleUserInfo, get_google_identity_verifier
from app.main import app
from app.presentation.api.v1 import auth as auth_api
from app.presentation.api.v1 import connectors as connectors_api
from app.presentation.api.v1 import local_agents as local_agents_api
from app.presentation.api.v1 import scans as scans_api
from app.presentation.api.v1 import users as users_api
from vault_shared import get_settings
from vault_shared.connectors.google_workspace import (
    GoogleAccountInfo,
    GoogleTokenSet,
    get_google_workspace_oauth_client,
)
from vault_shared.db.models import (
    AgentCommand,
    Conversation,
    DriveType,
    ExecutionJobStatus,
    File,
    Organization,
    ScanJob,
    StorageConnector,
    User,
)
from vault_shared.db.repositories import (
    AgentCommandRepository,
    ConnectorCredentialsRepository,
    ConversationRepository,
    ExecutionJobRepository,
    ExecutionPlanRepository,
    FileRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory
from vault_shared.security.encryption import decrypt_token


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except OSError:
        return False


requires_infra = pytest.mark.skipif(
    not (_reachable(get_settings().database_url) and _reachable(get_settings().redis_url)),
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI services.",
)


class _FakeGoogleSignIn:
    """Each ID token names its person: `id:<label>:<run>`. Google's real
    check (signature, audience, verified email) is unit-tested separately."""

    def verify(self, id_token_jwt: str) -> GoogleUserInfo:
        _, label, run = id_token_jwt.split(":")
        return GoogleUserInfo(
            sub=f"google-sub-{label}-{run}",
            email=f"{label}-{run}@example.com",
            email_verified=True,
            name=label.title(),
            picture=None,
        )


class _FakeGoogleDrive:
    """Hands each person their own Drive tokens: the authorization code
    names whose Drive was approved (`code:<label>`)."""

    def __init__(self) -> None:
        self.refreshed_with: list[str] = []
        self.revoked: list[str] = []

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, *, code: str) -> GoogleTokenSet:
        label = code.split(":", 1)[1]
        return GoogleTokenSet(
            access_token=f"access-{label}",
            refresh_token=f"refresh-{label}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="openid email profile https://www.googleapis.com/auth/drive",
        )

    def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenSet:
        self.refreshed_with.append(refresh_token)
        return GoogleTokenSet(
            access_token=f"{refresh_token}-renewed",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes="openid email profile https://www.googleapis.com/auth/drive",
        )

    def fetch_account_info(self, *, access_token: str) -> GoogleAccountInfo:
        label = access_token.removeprefix("access-")
        return GoogleAccountInfo(email=f"{label}-drive@example.com", workspace_domain=None)

    def revoke(self, *, token: str) -> bool:
        self.revoked.append(token)
        return True


class _Person:
    def __init__(self, client: TestClient, label: str) -> None:
        self.label = f"{label}{uuid.uuid4().hex[:8]}"
        self.client = client
        response = client.post("/v1/auth/login", json={"id_token": f"id:{self.label}:run"})
        assert response.status_code == 200, response.text
        self.session = response.json()
        self.email = self.session["user"]["email"]
        self.headers = {"Authorization": f"Bearer {self.session['access_token']}"}
        self.drive_id: str | None = None
        self.drive_source_id: uuid.UUID | None = None

    def connect_drive(self) -> dict:
        started = self.client.post("/v1/connectors/google/connect", headers=self.headers)
        assert started.status_code == 200, started.text
        state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]
        connected = self.client.post(
            "/v1/connectors/google/callback",
            headers=self.headers,
            json={"code": f"code:{self.label}", "state": state},
        )
        assert connected.status_code == 200, connected.text
        self.drive_id = connected.json()["id"]
        return connected.json()

    def get(self, url: str, **kwargs):  # noqa: ANN003, ANN201
        return self.client.get(url, headers=self.headers, **kwargs)

    def post(self, url: str, **kwargs):  # noqa: ANN003, ANN201
        return self.client.post(url, headers=self.headers, **kwargs)


@pytest.fixture(scope="module")
def drive() -> _FakeGoogleDrive:
    return _FakeGoogleDrive()


@pytest.fixture(scope="module")
def client(drive: _FakeGoogleDrive):
    overrides = {
        get_google_identity_verifier: lambda: _FakeGoogleSignIn(),
        get_google_workspace_oauth_client: lambda: drive,
        # Many sign-ins in one run would trip the per-IP limits; those are
        # tested on their own.
        auth_api._login_rate_limit: lambda: None,
        connectors_api._connect_rate_limit: lambda: None,
        connectors_api._callback_rate_limit: lambda: None,
        scans_api._start_scan_rate_limit: lambda: None,
        users_api._delete_rate_limit: lambda: None,
    }
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _add_files(db: Session, person: _Person, names: list[str]) -> list[File]:
    source = StorageSourceRepository(db).upsert(
        connector_id=uuid.UUID(person.drive_id),
        provider_drive_id=f"root-{person.label}",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    person.drive_source_id = source.id
    now = datetime.now(UTC)
    files = [
        FileRepository(db).upsert(
            storage_source_id=source.id,
            provider_file_id=f"{person.label}-{name}",
            provider_parent_id=None,
            parent_folder_id=None,
            name=name,
            path=f"/{name}",
            mime_type="text/plain",
            size_bytes=10,
            owner_email=None,
            is_shared=False,
            permissions_summary=None,
            version_id=None,
            checksum=None,
            web_view_link=None,
            provider_created_at=now,
            provider_modified_at=now,
            provider_viewed_at=None,
            scanned_at=now,
        )
        for name in names
    ]
    db.commit()
    return files


@pytest.fixture(scope="module")
def pair(client: TestClient):
    ada, bo = _Person(client, "ada"), _Person(client, "bo")
    ada.connect_drive()
    bo.connect_drive()
    return ada, bo


# -- sign-in and workspaces ------------------------------------------------------------


@requires_infra
def test_two_google_accounts_get_two_separate_accounts_and_workspaces(pair) -> None:  # noqa: ANN001
    ada, bo = pair

    assert ada.session["user"]["id"] != bo.session["user"]["id"]
    assert ada.session["user"]["organization_id"] != bo.session["user"]["organization_id"]
    assert ada.get("/v1/users/me").json()["email"] == ada.email
    assert bo.get("/v1/users/me").json()["email"] == bo.email


@requires_infra
def test_signing_in_again_returns_the_same_account(client: TestClient, pair) -> None:  # noqa: ANN001
    ada, _ = pair

    again = client.post("/v1/auth/login", json={"id_token": f"id:{ada.label}:run"}).json()

    assert again["user"]["id"] == ada.session["user"]["id"]


# -- Google Drive connections ------------------------------------------------------------


@requires_infra
def test_each_person_connects_their_own_drive(pair, db: Session) -> None:  # noqa: ANN001
    ada, bo = pair

    ada_list = ada.get("/v1/connectors").json()
    bo_list = bo.get("/v1/connectors").json()

    assert [c["id"] for c in ada_list] == [ada.drive_id]
    assert [c["id"] for c in bo_list] == [bo.drive_id]
    assert ada_list[0]["account_email"] == f"{ada.label}-drive@example.com"
    stored = ConnectorCredentialsRepository(db).get_by_connector_id(uuid.UUID(ada.drive_id))
    assert decrypt_token(stored.refresh_token_encrypted) == f"refresh-{ada.label}"
    assert stored.refresh_token_encrypted != f"refresh-{ada.label}"


@requires_infra
def test_a_drive_approval_cannot_be_completed_in_someone_elses_account(
    client: TestClient, pair  # noqa: ANN001
) -> None:
    _, bo = pair
    jo = _Person(client, "jo")
    started = jo.post("/v1/connectors/google/connect")
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]

    hijack = bo.post("/v1/connectors/google/callback", json={"code": "code:mallory", "state": state})

    assert hijack.status_code in (401, 403, 404)
    assert [c["account_email"] for c in bo.get("/v1/connectors").json()] == [
        f"{bo.label}-drive@example.com"
    ]


@requires_infra
def test_token_refresh_uses_each_persons_own_refresh_token(
    pair, db: Session, drive: _FakeGoogleDrive  # noqa: ANN001
) -> None:
    from app.application.connector_service import ConnectorService

    ada, bo = pair
    service = ConnectorService(db, oauth_client=drive)
    for person in (ada, bo):
        stored = ConnectorCredentialsRepository(db).get_by_connector_id(uuid.UUID(person.drive_id))
        stored.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    tokens = {
        person.label: service.get_valid_access_token(
            db.get(StorageConnector, uuid.UUID(person.drive_id))
        )
        for person in (ada, bo)
    }

    assert tokens == {
        ada.label: f"refresh-{ada.label}-renewed",
        bo.label: f"refresh-{bo.label}-renewed",
    }


# -- files and background jobs -----------------------------------------------------------


@requires_infra
def test_each_person_sees_only_their_own_files(pair, db: Session) -> None:  # noqa: ANN001
    ada, bo = pair
    [ada_file] = _add_files(db, ada, ["ada-plan.txt"])
    [bo_file] = _add_files(db, bo, ["bo-budget.txt"])

    ada_browse = ada.get(f"/v1/connectors/{ada.drive_id}/browse", params={"connector_id": ada.drive_id})
    names = {f["name"] for f in ada_browse.json()["files"]}

    assert "ada-plan.txt" in names and "bo-budget.txt" not in names
    assert ada.get(f"/v1/files/{ada_file.id}", params={"connector_id": ada.drive_id}).status_code == 200
    assert ada.get(f"/v1/files/{bo_file.id}", params={"connector_id": ada.drive_id}).status_code == 404
    assert ada.get(f"/v1/connectors/{bo.drive_id}/browse").status_code == 404


@requires_infra
def test_a_background_scan_carries_its_owners_connection(
    pair, db: Session, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    from app.application import scan_service

    queued: list[uuid.UUID] = []
    monkeypatch.setattr(scan_service, "enqueue_scan_job", queued.append)
    ada, bo = pair

    started = ada.post(f"/v1/connectors/{ada.drive_id}/scans", json={"scan_type": "full"})
    foreign = ada.post(f"/v1/connectors/{bo.drive_id}/scans", json={"scan_type": "full"})

    assert started.status_code == 201, started.text
    assert foreign.status_code == 404
    [job_id] = queued
    job = db.get(ScanJob, job_id)
    assert str(job.connector_id) == ada.drive_id
    assert bo.get(f"/v1/scans/{job_id}").status_code == 404


# -- this computer (Local Agent) ---------------------------------------------------------


@requires_infra
def test_each_computer_agent_works_only_for_its_owner(pair, db: Session) -> None:  # noqa: ANN001
    ada, bo = pair
    ada_key = ada.post("/v1/local-agents", json={"name": "Ada's laptop"}).json()
    bo_key = bo.post("/v1/local-agents", json={"name": "Bo's desktop"}).json()
    bo_command = AgentCommandRepository(db).enqueue(
        connector_id=uuid.UUID(bo_key["connector_id"]), op="list_roots", params={}
    )
    db.commit()

    ada_polls = ada.client.get(
        "/v1/agent/commands",
        params={"wait": 0},
        headers={"Authorization": f"Agent {ada_key['agent_key']}"},
    )
    steal = ada.client.post(
        f"/v1/agent/commands/{bo_command.id}/result",
        headers={"Authorization": f"Agent {ada_key['agent_key']}"},
        json={"ok": True},
    )

    assert ada_polls.status_code == 200
    assert str(bo_command.id) not in ada_polls.text
    assert steal.status_code == 404
    db.expire_all()
    assert db.get(AgentCommand, bo_command.id).status == "pending"
    assert [a["connector_id"] for a in ada.get("/v1/local-agents").json()] == [
        ada_key["connector_id"]
    ]


@requires_infra
def test_a_users_session_is_not_an_agent_key(pair) -> None:  # noqa: ANN001
    ada, _ = pair

    response = ada.client.get(
        "/v1/agent/commands",
        params={"wait": 0},
        headers={"Authorization": f"Agent {ada.session['access_token']}"},
    )

    assert response.status_code == 401


# -- disconnecting -----------------------------------------------------------------------


@requires_infra
def test_disconnecting_one_drive_leaves_the_others_connected(
    client: TestClient, drive: _FakeGoogleDrive, db: Session
) -> None:
    cy, dee = _Person(client, "cy"), _Person(client, "dee")
    cy.connect_drive()
    dee.connect_drive()

    response = cy.post(f"/v1/connectors/{cy.drive_id}/disconnect")

    assert response.status_code == 200, response.text
    assert f"refresh-{cy.label}" in drive.revoked
    assert f"refresh-{dee.label}" not in drive.revoked
    assert ConnectorCredentialsRepository(db).get_by_connector_id(uuid.UUID(cy.drive_id)) is None
    dee_connection = dee.get("/v1/connectors").json()[0]
    assert dee_connection["status"] == "connected"
    assert ConnectorCredentialsRepository(db).get_by_connector_id(uuid.UUID(dee.drive_id))


@requires_infra
def test_nobody_can_disconnect_someone_elses_drive(pair) -> None:  # noqa: ANN001
    ada, bo = pair

    response = ada.post(f"/v1/connectors/{bo.drive_id}/disconnect")

    assert response.status_code == 404
    assert bo.get("/v1/connectors").json()[0]["status"] == "connected"


# -- deleting an account -----------------------------------------------------------------


@requires_infra
def test_deleting_an_account_removes_only_that_persons_data(
    client: TestClient, drive: _FakeGoogleDrive, db: Session
) -> None:
    eve, fay = _Person(client, "eve"), _Person(client, "fay")
    eve.connect_drive()
    fay.connect_drive()
    [eve_file] = _add_files(db, eve, ["eve-notes.txt"])
    [fay_file] = _add_files(db, fay, ["fay-notes.txt"])
    eve_agent = eve.post("/v1/local-agents", json={"name": "Eve's laptop"}).json()
    for person in (eve, fay):
        ConversationRepository(db).create(
            organization_id=uuid.UUID(person.session["user"]["organization_id"]),
            user_id=uuid.UUID(person.session["user"]["id"]),
            title="About my files",
            connector_id=uuid.UUID(person.drive_id),
        )
    db.commit()
    eve_org = uuid.UUID(eve.session["user"]["organization_id"])
    eve_user = uuid.UUID(eve.session["user"]["id"])
    eve_file_id, fay_file_id = eve_file.id, fay_file.id

    response = eve.post("/v1/users/me/delete", json={"confirm_email": eve.email})

    assert response.status_code == 200, response.text
    assert response.json()["files_unindexed"] == 1
    assert f"refresh-{eve.label}" in drive.revoked
    db.expire_all()
    assert db.get(Organization, eve_org) is None
    assert db.get(User, eve_user) is None
    assert db.get(File, eve_file_id) is None
    assert db.query(Conversation).filter_by(organization_id=eve_org).count() == 0
    assert db.get(StorageConnector, uuid.UUID(eve_agent["connector_id"])) is None
    # The other person is untouched.
    assert db.get(File, fay_file_id) is not None
    assert fay.get("/v1/users/me").status_code == 200
    assert fay.get("/v1/connectors").json()[0]["status"] == "connected"
    assert f"refresh-{fay.label}" not in drive.revoked


@requires_infra
def test_a_deleted_account_can_no_longer_be_used(client: TestClient, db: Session) -> None:
    gil = _Person(client, "gil")
    agent = gil.post("/v1/local-agents", json={"name": "Gil's laptop"}).json()

    gil.post("/v1/users/me/delete", json={"confirm_email": gil.email})

    assert gil.get("/v1/users/me").status_code == 401
    assert (
        client.get(
            "/v1/agent/commands",
            params={"wait": 0},
            headers={"Authorization": f"Agent {agent['agent_key']}"},
        ).status_code
        == 401
    )


@requires_infra
def test_deletion_needs_the_accounts_email_typed_exactly(client: TestClient, db: Session) -> None:
    hal = _Person(client, "hal")

    response = hal.post("/v1/users/me/delete", json={"confirm_email": "someone@example.com"})

    assert response.status_code == 422
    assert db.get(User, uuid.UUID(hal.session["user"]["id"])) is not None


@requires_infra
def test_deletion_waits_while_changes_are_running_in_storage(
    client: TestClient, db: Session
) -> None:
    ivy = _Person(client, "ivy")
    ivy.connect_drive()
    organization_id = uuid.UUID(ivy.session["user"]["organization_id"])
    plan = ExecutionPlanRepository(db).create(
        organization_id=organization_id,
        created_by_user_id=uuid.UUID(ivy.session["user"]["id"]),
        target_provider="google_workspace",
        estimated_impact="1 file",
        estimated_storage_savings_bytes=None,
        risk_level="low",
        rollback_available=True,
        required_permissions=[],
    )
    job = ExecutionJobRepository(db).create(
        execution_plan_id=plan.id,
        organization_id=organization_id,
        triggered_by_user_id=uuid.UUID(ivy.session["user"]["id"]),
    )
    job.status = ExecutionJobStatus.RUNNING
    db.commit()

    response = ivy.post("/v1/users/me/delete", json={"confirm_email": ivy.email})

    assert response.status_code == 409
    assert db.get(User, uuid.UUID(ivy.session["user"]["id"])) is not None
