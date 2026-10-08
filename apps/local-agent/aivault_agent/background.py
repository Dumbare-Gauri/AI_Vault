"""The agent as it normally runs: started by Windows at sign-in (or by the
Start button in AI Vault), with no window. It waits until AI Vault pairs with
it, then serves; a new key from AI Vault takes over without a restart.
"""

import sys
import threading

from aivault_agent.agent_service import AgentService
from aivault_agent.cloud import CloudClient, CloudError, serve
from aivault_agent.pairing import PAIRING_PORT, PairingServer
from aivault_agent.settings import RootsFile, default_roots_file, settings_folder

_RETRY_SECONDS = 30
_MAX_LOG_BYTES = 1024 * 1024


def _log_to_file() -> None:
    """Without a console (pythonw) there is nowhere to print; keep a small log."""
    if sys.stdout is not None:
        return
    log = settings_folder() / "agent.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if log.exists() and log.stat().st_size > _MAX_LOG_BYTES else "a"
    stream = open(log, mode, encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stderr = stream


def run_background(settings: RootsFile, *, port: int = PAIRING_PORT) -> int:
    paired = threading.Event()
    try:
        pairing = PairingServer(settings, on_paired=paired.set, port=port)
    except OSError:
        print("[aivault-agent] already running", flush=True)
        return 0
    threading.Thread(target=pairing.serve_forever, daemon=True).start()
    agent = AgentService([], roots_file=settings)
    print("[aivault-agent] started", flush=True)
    while True:
        paired.clear()
        values = settings.values()
        token, server = values.get("token"), values.get("server")
        if not token or not server:
            print("[aivault-agent] waiting for AI Vault to connect this computer", flush=True)
            paired.wait()
            continue
        try:
            serve(CloudClient(server, token), agent, stop=paired)
        except CloudError as error:
            print(f"[aivault-agent] {error}", flush=True)
            # A rejected key waits for AI Vault to pair again; anything else retries.
            paired.wait(None if error.status == 401 else _RETRY_SECONDS)


def main() -> None:
    _log_to_file()
    sys.exit(run_background(default_roots_file()))
