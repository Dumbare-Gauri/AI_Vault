"""The agent's settings on this computer: the folders the user gave it, and —
once AI Vault paired with it — where AI Vault is and the agent's key.

Kept in the user's own settings folder, never alongside their files. A
folder on a drive that is unplugged stays remembered, so it comes back when
the drive does.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any


class RootsFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def values(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def update(self, **values: Any) -> None:
        data = {**self.values(), **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(pending, self.path)

    def load(self) -> list[str]:
        roots = self.values().get("roots", [])
        return [root for root in roots if isinstance(root, str)] if isinstance(roots, list) else []

    def save(self, roots: list[str]) -> None:
        self.update(roots=roots)


def settings_folder() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "AIVault"


def default_roots_file() -> RootsFile:
    return RootsFile(settings_folder() / "agent.json")
