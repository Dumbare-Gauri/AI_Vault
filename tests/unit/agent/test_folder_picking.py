import os
import sys
from pathlib import Path

import pytest
from aivault_agent.agent_service import AgentService
from aivault_agent.settings import RootsFile


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    tmp_path = tmp_path / "disk"
    (tmp_path / "Clients" / "Acme").mkdir(parents=True)
    (tmp_path / "Photos").mkdir()
    (tmp_path / "notes.txt").write_text("not a folder")
    return tmp_path


@pytest.fixture
def agent(tmp_path: Path) -> AgentService:
    return AgentService([], roots_file=RootsFile(tmp_path / "settings" / "agent.json"))


def test_browsing_lists_only_the_folders_inside(agent: AgentService, disk: Path) -> None:
    result = agent.handle({"op": "browse", "params": {"path": str(disk)}})

    assert [folder["name"] for folder in result["folders"]] == ["Clients", "Photos"]
    assert result["folders"][0]["path"] == str(disk / "Clients")
    assert result["folders"][0]["can_add"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system folders")
def test_browsing_the_system_drive_marks_windows_as_not_addable(agent: AgentService) -> None:
    drive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"

    result = agent.handle({"op": "browse", "params": {"path": drive}})

    windows = next(f for f in result["folders"] if f["name"].lower() == "windows")
    assert windows["can_add"] is False
    assert "system" in windows["reason"]


def test_browsing_needs_an_absolute_path(agent: AgentService) -> None:
    result = agent.handle({"op": "browse", "params": {"path": "relative/folder"}})

    assert result["ok"] is False
    assert result["kind"] == "refused"


def test_adding_a_folder_authorizes_and_remembers_it(agent: AgentService, disk: Path) -> None:
    result = agent.handle({"op": "add_root", "params": {"path": str(disk / "Clients")}})

    assert result["ok"] is True
    assert result["roots"] == [str((disk / "Clients").resolve())]
    assert agent.handle({"op": "scan", "params": {"root": str(disk / "Clients")}})["ok"] is True
    assert agent.roots_file.load() == [str((disk / "Clients").resolve())]


def test_a_system_folder_cannot_be_added(agent: AgentService) -> None:
    system = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        if sys.platform == "win32"
        else Path("/usr")
    )

    result = agent.handle({"op": "add_root", "params": {"path": str(system)}})

    assert result["ok"] is False
    assert "system" in result["error"]


def test_the_system_drive_itself_cannot_be_added(agent: AgentService) -> None:
    result = agent.handle({"op": "add_root", "params": {"path": Path.home().anchor}})

    assert result["ok"] is False
    assert "whole" in result["error"]


def test_a_folder_inside_an_added_folder_is_already_covered(
    agent: AgentService, disk: Path
) -> None:
    agent.handle({"op": "add_root", "params": {"path": str(disk / "Clients")}})

    result = agent.handle({"op": "add_root", "params": {"path": str(disk / "Clients" / "Acme")}})

    assert result["ok"] is False
    assert "already" in result["error"]


def test_removing_a_folder_stops_access_but_leaves_the_files(
    agent: AgentService, disk: Path
) -> None:
    agent.handle({"op": "add_root", "params": {"path": str(disk / "Clients")}})

    result = agent.handle({"op": "remove_root", "params": {"path": str(disk / "Clients")}})

    assert result == {"ok": True, "roots": []}
    assert agent.handle({"op": "scan", "params": {"root": str(disk / "Clients")}})["ok"] is False
    assert (disk / "Clients" / "Acme").is_dir()
    assert agent.roots_file.load() == []


def test_remembered_folders_come_back_after_a_restart(tmp_path: Path, disk: Path) -> None:
    roots_file = RootsFile(tmp_path / "settings" / "agent.json")
    AgentService([], roots_file=roots_file).handle(
        {"op": "add_root", "params": {"path": str(disk / "Photos")}}
    )

    restarted = AgentService([], roots_file=roots_file)

    assert restarted.authorized.roots == [(disk / "Photos").resolve()]


def test_an_unplugged_drive_is_remembered_and_picked_up_when_it_returns(
    tmp_path: Path, disk: Path
) -> None:
    roots_file = RootsFile(tmp_path / "settings" / "agent.json")
    roots_file.save([str(disk / "Photos"), str(disk / "External")])

    agent = AgentService([], roots_file=roots_file)
    (disk / "External").mkdir()
    returned = agent.recheck_missing_roots()

    assert returned == [(disk / "External").resolve()]
    assert (disk / "External").resolve() in agent.authorized.roots
    assert roots_file.load() == [str(disk / "Photos"), str(disk / "External")]
