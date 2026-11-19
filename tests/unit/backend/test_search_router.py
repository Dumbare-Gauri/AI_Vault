import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.application.search_service import SearchOutcome, SearchResult
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_search_service

client = TestClient(app)


class _FakeFile:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "Q3 Board Deck.pdf"
        self.path = "/Finance/Q3 Board Deck.pdf"
        self.mime_type = "application/pdf"
        self.size_bytes = 2048
        self.provider_modified_at = None
        self.web_view_link = None


@pytest.fixture
def fake_search_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_search_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_search_service, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_search_requires_authentication(fake_search_service) -> None:
    response = client.post("/v1/search", json={"query": "board deck"})
    assert response.status_code == 401


def test_search_returns_ranked_results(as_member, fake_search_service) -> None:
    file = _FakeFile()
    fake_search_service.find.return_value = SearchOutcome(
        results=[SearchResult(file=file, score=0.87, retrieval_method="both")],
        total=1,
        understood=["matching “board deck”"],
        interpreted_by_ai=False,
    )

    response = client.post("/v1/search", json={"query": "board deck"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "board deck"
    assert len(body["results"]) == 1
    assert body["results"][0]["file_id"] == str(file.id)
    assert body["results"][0]["retrieval_method"] == "both"
    assert body["results"][0]["score"] == 0.87
    assert body["understood"] == ["matching “board deck”"]


def test_search_rejects_an_empty_query(as_member, fake_search_service) -> None:
    response = client.post("/v1/search", json={"query": ""})

    assert response.status_code == 422
    fake_search_service.find.assert_not_called()


def test_search_returns_empty_results_when_nothing_matches(as_member, fake_search_service) -> None:
    fake_search_service.find.return_value = SearchOutcome(
        results=[], total=0, understood=[], interpreted_by_ai=False
    )

    response = client.post("/v1/search", json={"query": "nonexistent topic"})

    assert response.status_code == 200
    assert response.json()["results"] == []
