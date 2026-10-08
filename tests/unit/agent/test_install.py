import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest
from aivault_agent import background
from aivault_agent.install import install, uninstall
from aivault_agent.settings import RootsFile

APP = "http://localhost:5173"


class _FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_value(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def delete_key(self, key: str) -> None:
        self.values = {k: v for k, v in self.values.items() if not k[0].startswith(key)}

    def delete_value(self, key: str, name: str) -> None:
        self.values.pop((key, name), None)


@pytest.fixture
def setup(tmp_path: Path):
    settings = RootsFile(tmp_path / "agent.json")
    launcher = tmp_path / "start-agent.pyw"
    registry = _FakeRegistry()
    command = install(
        server="http://localhost:8000/",
        app_origin=APP,
        settings=settings,
        launcher=launcher,
        registry=registry,
        python=Path("C:/Python/pythonw.exe"),
    )
    return settings, launcher, registry, command


def test_install_starts_the_agent_at_sign_in(setup) -> None:  # noqa: ANN001
    _, launcher, registry, command = setup

    run = registry.values[(r"Software\Microsoft\Windows\CurrentVersion\Run", "AI Vault Agent")]
    assert run == command
    assert str(launcher) in command and "pythonw" in command
    assert "aivault_agent.background" in launcher.read_text(encoding="utf-8")


def test_install_registers_the_start_link_without_passing_its_contents(setup) -> None:  # noqa: ANN001
    _, _, registry, command = setup

    open_command = registry.values[(r"Software\Classes\aivault-agent\shell\open\command", "")]
    assert open_command == command
    assert "%1" not in open_command
    assert (r"Software\Classes\aivault-agent", "URL Protocol") in registry.values


def test_install_fixes_which_ai_vault_the_agent_belongs_to(setup) -> None:  # noqa: ANN001
    settings, _, _, _ = setup

    assert settings.values() == {"server": "http://localhost:8000", "app_origin": APP}


def test_uninstall_removes_the_autostart_the_link_and_the_key(setup) -> None:  # noqa: ANN001
    settings, launcher, registry, _ = setup
    settings.update(token="secret")

    uninstall(settings=settings, launcher=launcher, registry=registry)

    assert registry.values == {}
    assert not launcher.exists()
    assert settings.values()["token"] is None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_agent_waits_for_pairing_then_serves_with_the_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RootsFile(tmp_path / "agent.json")
    settings.update(server="http://localhost:8000", app_origin=APP)
    port = _free_port()
    served: list[str] = []

    def fake_serve(client, agent, stop) -> None:  # noqa: ANN001
        served.append(client._token)
        raise KeyboardInterrupt

    monkeypatch.setattr(background, "serve", fake_serve)
    runner = threading.Thread(
        target=lambda: pytest.raises(
            KeyboardInterrupt, background.run_background, settings, port=port
        ),
        daemon=True,
    )
    runner.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/pair",
        data=json.dumps({"token": "fresh-key"}).encode(),
        method="POST",
        headers={"Origin": APP, "Content-Type": "application/json"},
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(request, timeout=2).close()
            break
        except OSError:
            threading.Event().wait(0.1)
    runner.join(timeout=5)

    assert served == ["fresh-key"]


def test_a_second_agent_exits_quietly_while_one_is_running(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        result = background.run_background(RootsFile(tmp_path / "agent.json"), port=port)

    assert result == 0
