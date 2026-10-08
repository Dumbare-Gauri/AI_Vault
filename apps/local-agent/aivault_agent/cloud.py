"""The agent's connection to AI Vault.

The agent only ever connects out: it reports what it sees, asks for queued
commands, runs each through `AgentService` (a fixed table of typed
operations) and reports the result. AI Vault never connects into the machine
and never sends anything that is executed as code.
"""

import json
import platform
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from urllib.parse import urlparse

from aivault_agent.agent_service import AgentService, _iso
from aivault_agent.scanner import ScanResult
from aivault_agent.volumes import list_volumes

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_SCAN_BATCH = 500
_HEARTBEAT_SECONDS = 30
_POLL_WAIT_SECONDS = 20


class CloudError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CloudClient:
    def __init__(self, server_url: str, token: str, *, timeout: int = 60) -> None:
        parsed = urlparse(server_url)
        if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
            raise CloudError("AI Vault must be reached over HTTPS.")
        self._base = server_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def request(
        self, method: str, path: str, body: dict | None = None, *, timeout: int | None = None
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Agent {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:  # noqa: S310 - scheme checked above
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise CloudError(
                f"AI Vault answered {error.code} for {path}", status=error.code
            ) from error
        except OSError as error:
            raise CloudError(f"Could not reach AI Vault: {error}") from error
        return json.loads(payload) if payload else {}


def heartbeat(client: CloudClient, agent: AgentService) -> None:
    """Every drive is reported, external ones included, so the user can pick
    a folder on any of them."""
    client.request(
        "POST",
        "/v1/agent/heartbeat",
        {
            "device_name": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()}",
            "roots": [str(root) for root in agent.authorized.roots],
            "offline_roots": agent.missing_roots(),
            "volumes": [asdict(volume) for volume in list_volumes()],
        },
    )


def upload_scan(client: CloudClient, result: ScanResult, summary: dict) -> None:
    """Sends a scan's entries in batches; the final batch tells AI Vault the
    scan is complete, so it can mark what disappeared."""
    entries = [
        {key: _iso(value) for key, value in asdict(entry).items()} for entry in result.entries
    ]
    batches = [entries[i : i + _SCAN_BATCH] for i in range(0, len(entries), _SCAN_BATCH)] or [[]]
    for number, batch in enumerate(batches):
        client.request(
            "POST",
            "/v1/agent/scan",
            {
                "root": summary["root"],
                "root_id": summary["root_id"],
                "entries": batch,
                "first": number == 0,
                "final": number == len(batches) - 1,
                "errors": summary["errors"] if number == len(batches) - 1 else [],
            },
        )


class BackgroundScans:
    """Scans folders one at a time on their own thread, so a big folder never
    holds up commands or heartbeats."""

    def __init__(self, client: CloudClient, agent: AgentService, stop: threading.Event) -> None:
        self._client = client
        self._agent = agent
        self._stop = stop
        self._queue: queue.Queue[str] = queue.Queue()
        self._waiting: set[str] = set()
        self._guard = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, root: str) -> None:
        with self._guard:
            if root in self._waiting:
                return
            self._waiting.add(root)
        self._queue.put(root)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                root = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            with self._guard:
                self._waiting.discard(root)
            try:
                print(f"[aivault-agent] scanning {root}", flush=True)
                summary, result = self._agent.scan_root(root)
                upload_scan(self._client, result, summary)
                print(f"[aivault-agent] scanned {root}: {summary['entries']} items", flush=True)
            except Exception as error:  # noqa: BLE001 - one folder failing must not stop the rest
                print(f"[aivault-agent] couldn't scan {root}: {error}", flush=True)


def run_command(
    client: CloudClient, agent: AgentService, command: dict, scans: BackgroundScans
) -> None:
    op = command.get("op")
    params = command.get("params")
    root = params.get("root") if isinstance(params, dict) else None
    if op == "scan" and isinstance(root, str):
        if not agent.authorized.covers(root):
            result = {"ok": False, "kind": "refused", "error": "That folder isn't authorized."}
        else:
            scans.request(root)
            result = {"ok": True, "queued": True}
    else:
        result = agent.handle({"op": op, "params": params})
    if op in ("add_root", "remove_root") and result.get("ok"):
        heartbeat(client, agent)
    client.request("POST", f"/v1/agent/commands/{command['id']}/result", result)
    if op == "add_root" and result.get("ok"):
        scans.request(result["root"])


def serve(client: CloudClient, agent: AgentService, stop: threading.Event | None = None) -> None:
    """Runs until interrupted or `stop` is set: scan every authorized folder
    once, then keep reporting in and running whatever AI Vault queues. A key
    AI Vault no longer accepts ends it, since retrying can't help."""
    stop = stop or threading.Event()
    heartbeat(client, agent)
    scans = BackgroundScans(client, agent, stop)
    for root in agent.authorized.roots:
        scans.request(str(root))
    last_heartbeat = time.monotonic()
    while not stop.is_set():
        try:
            if time.monotonic() - last_heartbeat > _HEARTBEAT_SECONDS:
                returned = agent.recheck_missing_roots()
                heartbeat(client, agent)
                last_heartbeat = time.monotonic()
                for root in returned:
                    scans.request(str(root))
            reply = client.request(
                "GET",
                f"/v1/agent/commands?wait={_POLL_WAIT_SECONDS}",
                timeout=_POLL_WAIT_SECONDS + 15,
            )
            for command in reply.get("commands", []):
                run_command(client, agent, command, scans)
        except CloudError as error:
            if error.status == 401:
                raise
            print(f"[aivault-agent] {error} — retrying in 10s", flush=True)
            stop.wait(10)
