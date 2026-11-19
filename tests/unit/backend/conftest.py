import uuid
from types import SimpleNamespace

import pytest
from factories import make_organization, make_role, make_user
from vault_shared.db.models import Organization, Role, User


@pytest.fixture
def organization() -> Organization:
    return make_organization()


@pytest.fixture
def owner_role() -> Role:
    return make_role("owner")


@pytest.fixture
def member_role() -> Role:
    return make_role("member")


@pytest.fixture
def owner_user(organization: Organization, owner_role: Role) -> User:
    return make_user(organization=organization, role=owner_role)


@pytest.fixture
def member_user(organization: Organization, member_role: Role) -> User:
    return make_user(organization=organization, role=member_role, email="member@example.com")


# Every workspace-scoped route resolves a storage workspace from the database;
# router unit tests run without one, so they get this stand-in. Its id is what
# the routes must pass on to their services.
WORKSPACE = SimpleNamespace(
    id=uuid.UUID("00000000-0000-0000-0000-00000000aaaa"),
    provider="google_workspace",
    status="connected",
)


@pytest.fixture(autouse=True)
def workspace():
    from app.main import app
    from app.presentation.dependencies.workspace import get_workspace

    app.dependency_overrides[get_workspace] = lambda: WORKSPACE
    yield WORKSPACE
    app.dependency_overrides.pop(get_workspace, None)
