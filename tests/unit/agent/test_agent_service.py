from pathlib import Path

import pytest
from aivault_agent.agent_service import AgentService


@pytest.fixture
def agent(tmp_path: Path) -> AgentService:
    root = tmp_path / "work"
    (root / "Inbox").mkdir(parents=True)
    (root / "Inbox" / "proposal-final2.pdf").write_bytes(b"%PDF v2")
    service = AgentService([root])
    service.handle({"op": "scan", "params": {"root": str(root)}})
    return service


def _id(agent: AgentService, name: str) -> str:
    return next(fid for fid, place in agent.index.items() if place.relative_path.endswith(name))


def test_unknown_operations_are_refused(agent: AgentService) -> None:
    result = agent.handle({"op": "shell", "params": {"command": "rm -rf /"}})

    assert result == {"ok": False, "kind": "refused", "error": "Unknown operation: shell"}


def test_unexpected_parameters_are_refused(agent: AgentService) -> None:
    file_id = _id(agent, "proposal-final2.pdf")

    result = agent.handle(
        {"op": "rename", "params": {"id": file_id, "new_name": "x.pdf", "path": "/etc/passwd"}}
    )

    assert result["ok"] is False
    assert "path" in result["error"]


def test_rename_by_id_reports_the_new_state(agent: AgentService) -> None:
    file_id = _id(agent, "proposal-final2.pdf")

    result = agent.handle({"op": "rename", "params": {"id": file_id, "new_name": "Final.pdf"}})

    assert result["ok"] is True
    assert result["file"]["name"] == "Final.pdf"
    assert result["file"]["id"] == file_id
    get = agent.handle({"op": "get", "params": {"id": file_id}})
    assert get["file"]["relative_path"] == "Inbox/Final.pdf"


def test_trash_and_restore_by_id_keep_the_identity(agent: AgentService) -> None:
    file_id = _id(agent, "proposal-final2.pdf")

    trashed = agent.handle({"op": "trash", "params": {"id": file_id}})
    seen = agent.handle({"op": "get", "params": {"id": file_id}})
    restored = agent.handle({"op": "restore", "params": {"id": file_id}})

    assert trashed["file"]["trashed"] is True
    assert seen["file"]["trashed"] is True
    assert restored["file"]["trashed"] is False
    assert restored["file"]["relative_path"] == "Inbox/proposal-final2.pdf"


def test_permanent_delete_only_after_trash(agent: AgentService) -> None:
    file_id = _id(agent, "proposal-final2.pdf")

    refused = agent.handle({"op": "permanent_delete", "params": {"id": file_id, "confirm": True}})
    agent.handle({"op": "trash", "params": {"id": file_id}})
    deleted = agent.handle({"op": "permanent_delete", "params": {"id": file_id, "confirm": True}})
    gone = agent.handle({"op": "get", "params": {"id": file_id}})

    assert refused["ok"] is False and "Trash" in refused["error"]
    assert deleted["ok"] is True
    assert gone == {
        "ok": False,
        "kind": "not_found",
        "error": "That file no longer exists on disk.",
    }


def test_a_path_outside_the_authorized_folder_is_refused(
    agent: AgentService, tmp_path: Path
) -> None:
    result = agent.handle({"op": "scan", "params": {"root": str(tmp_path)}})

    assert result["ok"] is False
    assert result["kind"] == "refused"


def test_volumes_are_reported(agent: AgentService) -> None:
    result = agent.handle({"op": "volumes", "params": {}})

    assert result["ok"] is True
    assert result["volumes"][0]["total_bytes"] > 0
