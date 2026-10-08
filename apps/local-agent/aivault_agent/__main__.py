"""Command line for the AI Vault Local Agent.

    py -m aivault_agent install      one time: start with Windows, add the Start link
    py -m aivault_agent uninstall
    py -m aivault_agent background   what Windows runs; waits for AI Vault to pair
    py -m aivault_agent scan --root D:\\Projects

After `install` nothing is typed again: AI Vault pairs with the agent and the
user picks folders and drives in AI Vault.
"""

import argparse
import json
import sys
from pathlib import Path

from aivault_agent.agent_service import AgentService
from aivault_agent.background import main as run_in_background
from aivault_agent.install import WindowsRegistry, install, start_now, uninstall, windowless_python
from aivault_agent.safety import PathRefused
from aivault_agent.settings import default_roots_file, settings_folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aivault_agent")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="start the agent with Windows")
    setup.add_argument("--server", default="http://localhost:8000")
    setup.add_argument("--app", default="http://localhost:5173")
    commands.add_parser("uninstall", help="stop starting the agent with Windows")
    commands.add_parser("background", help="run until stopped, paired from AI Vault")
    local = commands.add_parser("scan", help="scan locally and print a summary")
    local.add_argument("--root", action="append", required=True, type=Path)
    args = parser.parse_args(argv)

    launcher = settings_folder() / "start-agent.pyw"
    if args.command == "install":
        if sys.platform != "win32":
            print("Automatic start is set up on Windows only so far.", file=sys.stderr)
            return 2
        python = windowless_python()
        install(
            server=args.server,
            app_origin=args.app,
            settings=default_roots_file(),
            launcher=launcher,
            registry=WindowsRegistry(),
            python=python,
        )
        start_now(python, launcher)
        print("AI Vault Agent installed and running. It starts by itself when you sign in.")
        print("Now open AI Vault > Storage Connections > Connect this computer.")
        return 0
    if args.command == "uninstall":
        uninstall(settings=default_roots_file(), launcher=launcher, registry=WindowsRegistry())
        print("AI Vault Agent will no longer start with Windows.")
        return 0
    if args.command == "background":
        run_in_background()
        return 0

    try:
        agent = AgentService(args.root)
    except PathRefused as error:
        print(error, file=sys.stderr)
        return 2
    for root in agent.authorized.roots:
        print(json.dumps(agent.handle({"op": "scan", "params": {"root": str(root)}}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
