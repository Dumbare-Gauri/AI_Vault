"""One-time setup on Windows, so the agent never needs a command again.

- Starts the agent whenever the user signs in to Windows (no window).
- Registers the `aivault-agent://` link, so AI Vault's Start button can
  launch the agent if it isn't running. The link's contents are never passed
  to the agent — opening it only starts the agent.
- Records which AI Vault this agent belongs to; pairing can't change it.
"""

import subprocess
import sys
from pathlib import Path
from typing import Protocol

from aivault_agent.settings import RootsFile

URL_SCHEME = "aivault-agent"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "AI Vault Agent"
_LAUNCHER = """# Starts the AI Vault agent. Written by `aivault_agent install`.
import sys

sys.path.insert(0, {package_dir!r})
from aivault_agent.background import main

main()
"""


class Registry(Protocol):
    def set_value(self, key: str, name: str, value: str) -> None: ...
    def delete_key(self, key: str) -> None: ...
    def delete_value(self, key: str, name: str) -> None: ...


class WindowsRegistry:
    """The current user's registry hive only — no administrator rights."""

    def set_value(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete_key(self, key: str) -> None:
        import winreg

        for sub in ("shell\\open\\command", "shell\\open", "shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{key}\\{sub}".rstrip("\\"))
            except FileNotFoundError:
                continue

    def delete_value(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            return


def windowless_python() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def install(
    *,
    server: str,
    app_origin: str,
    settings: RootsFile,
    launcher: Path,
    registry: Registry,
    python: Path,
) -> str:
    """Returns the command Windows will run to start the agent."""
    settings.update(server=server.rstrip("/"), app_origin=app_origin.rstrip("/"))
    launcher.parent.mkdir(parents=True, exist_ok=True)
    package_dir = str(Path(__file__).resolve().parent.parent)
    launcher.write_text(_LAUNCHER.format(package_dir=package_dir), encoding="utf-8")
    command = f'"{python}" "{launcher}"'
    registry.set_value(_RUN_KEY, _RUN_VALUE, command)
    scheme_key = f"Software\\Classes\\{URL_SCHEME}"
    registry.set_value(scheme_key, "", "URL:AI Vault Agent")
    registry.set_value(scheme_key, "URL Protocol", "")
    registry.set_value(f"{scheme_key}\\shell\\open\\command", "", command)
    return command


def uninstall(*, settings: RootsFile, launcher: Path, registry: Registry) -> None:
    registry.delete_value(_RUN_KEY, _RUN_VALUE)
    registry.delete_key(f"Software\\Classes\\{URL_SCHEME}")
    launcher.unlink(missing_ok=True)
    values = settings.values()
    if "token" in values:
        settings.update(token=None)


def start_now(python: Path, launcher: Path) -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(  # noqa: S603 - fixed program and file, no user input
        [str(python), str(launcher)],
        creationflags=flags,
        close_fds=True,
    )
