from app.core.error_handlers import register_error_handlers
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vault_shared import AIUnavailableError, NotFoundError


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom/typed")
    def raise_typed_error() -> None:
        raise NotFoundError("widget not found", details={"widget_id": "abc"})

    @app.get("/boom/unexpected")
    def raise_unexpected_error() -> None:
        raise RuntimeError("something exploded")

    return app


client = TestClient(_build_test_app(), raise_server_exceptions=False)


def test_typed_vault_error_maps_to_its_declared_status_and_shape() -> None:
    response = client.get("/boom/typed")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "widget not found",
            "details": {"widget_id": "abc"},
        }
    }


def test_unexpected_exception_is_hidden_behind_a_generic_500() -> None:
    response = client.get("/boom/unexpected")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "something exploded" not in body["error"]["message"]


def test_an_ai_outage_is_a_503_with_a_reason_never_a_401() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom/ai")
    def raise_ai_error() -> None:
        raise AIUnavailableError("AI key rejected", reason=AIUnavailableError.AUTH_FAILED)

    response = TestClient(app, raise_server_exceptions=False).get("/boom/ai")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_unavailable"
    assert response.json()["error"]["details"]["reason"] == "auth_failed"


def test_unknown_route_returns_standardized_error_shape() -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
