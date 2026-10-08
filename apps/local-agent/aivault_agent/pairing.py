"""How the AI Vault page hands the agent its key, with no copying or typing.

A tiny HTTP server on the loopback interface only. It answers two questions
from one web origin — the AI Vault app this agent was installed for — and
nothing else: "is the agent here?" and "here is your key". The server the
agent talks to was fixed at install time and can't be changed from here, so
no website can redirect the agent. The server also doubles as the
single-instance lock: a second agent can't bind the same port.
"""

import json
import socket
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aivault_agent.settings import RootsFile

PAIRING_PORT = 47823
_MAX_BODY_BYTES = 4096
_MAX_TOKEN_LENGTH = 200


class PairingServer(ThreadingHTTPServer):
    daemon_threads = True
    # The port is the single-instance lock, so it must never be shared.
    allow_reuse_address = False

    def __init__(
        self,
        settings: RootsFile,
        *,
        on_paired: Callable[[], None],
        port: int = PAIRING_PORT,
    ) -> None:
        self.settings = settings
        self.on_paired = on_paired
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: PairingServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        expected = self.server.settings.values().get("app_origin")
        return origin if origin and expected and origin == expected else None

    def _reply(self, status: int, body: dict | None = None) -> None:
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        self.send_response(status)
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server's naming
        self._reply(204 if self._allowed_origin() else 403)

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed_origin():
            self._reply(403)
        elif self.path == "/status":
            values = self.server.settings.values()
            self._reply(
                200,
                {
                    "installed": True,
                    "paired": bool(values.get("token")),
                    "device_name": socket.gethostname(),
                },
            )
        else:
            self._reply(404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed_origin():
            self._reply(403)
            return
        if self.path != "/pair":
            self._reply(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._reply(400, {"error": "Send the agent key."})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reply(400, {"error": "Send the agent key."})
            return
        token = body.get("token") if isinstance(body, dict) else None
        if set(body) != {"token"} or not isinstance(token, str) or not token:
            self._reply(400, {"error": "Only the agent key can be sent."})
            return
        if len(token) > _MAX_TOKEN_LENGTH:
            self._reply(400, {"error": "That isn't an agent key."})
            return
        self.server.settings.update(token=token)
        self.server.on_paired()
        self._reply(200, {"ok": True})
