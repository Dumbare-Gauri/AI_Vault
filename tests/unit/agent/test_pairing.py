import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from aivault_agent.pairing import PairingServer
from aivault_agent.settings import RootsFile

APP = "http://localhost:5173"


@pytest.fixture
def settings(tmp_path: Path) -> RootsFile:
    settings = RootsFile(tmp_path / "agent.json")
    settings.update(server="http://localhost:8000", app_origin=APP)
    return settings


@pytest.fixture
def pairing(settings: RootsFile):
    paired = threading.Event()
    server = PairingServer(settings, on_paired=paired.set, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.paired_event = paired
    yield server
    server.shutdown()
    server.server_close()


def _call(server: PairingServer, method: str, path: str, *, origin: str, body: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Origin": origin, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read()
            return response.status, dict(response.headers), json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), None


def test_ai_vault_can_see_the_agent_is_installed(pairing: PairingServer) -> None:
    status, headers, body = _call(pairing, "GET", "/status", origin=APP)

    assert status == 200
    assert body["paired"] is False
    assert headers["Access-Control-Allow-Origin"] == APP


def test_pairing_stores_the_key_and_wakes_the_agent(
    pairing: PairingServer, settings: RootsFile
) -> None:
    status, _, body = _call(pairing, "POST", "/pair", origin=APP, body={"token": "k" * 43})

    assert status == 200 and body == {"ok": True}
    assert settings.values()["token"] == "k" * 43
    assert pairing.paired_event.is_set()


def test_another_website_cannot_pair_the_agent(pairing: PairingServer, settings: RootsFile) -> None:
    status, _, _ = _call(
        pairing, "POST", "/pair", origin="https://evil.example", body={"token": "x" * 43}
    )

    assert status == 403
    assert "token" not in settings.values()


def test_a_request_without_an_origin_is_refused(pairing: PairingServer) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{pairing.server_address[1]}/pair",
        data=b'{"token": "x"}',
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)

    assert error.value.code == 403


def test_pairing_cannot_point_the_agent_at_another_server(
    pairing: PairingServer, settings: RootsFile
) -> None:
    status, _, _ = _call(
        pairing,
        "POST",
        "/pair",
        origin=APP,
        body={"token": "k" * 43, "server": "https://evil.example"},
    )

    assert status == 400
    assert settings.values()["server"] == "http://localhost:8000"


def test_the_browser_preflight_is_answered_for_ai_vault_only(pairing: PairingServer) -> None:
    status, headers, _ = _call(pairing, "OPTIONS", "/pair", origin=APP)

    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == APP
    assert headers["Access-Control-Allow-Private-Network"] == "true"
