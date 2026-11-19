from pathlib import Path

import pytest
from aivault_agent.agent_service import AgentService


@pytest.fixture
def agent(tmp_path: Path) -> AgentService:
    root = tmp_path / "work"
    root.mkdir()
    (root / "old.txt").write_text("old")
    (root / "keep.txt").write_text("keep")
    service = AgentService([root])
    service.handle({"op": "scan", "params": {"root": str(root)}})
    return service


def _id(agent: AgentService, name: str) -> str:
    return next(fid for fid, place in agent.index.items() if place.relative_path == name)


def test_trash_contents_lists_what_emptying_would_delete(agent: AgentService) -> None:
    agent.handle({"op": "trash", "params": {"id": _id(agent, "old.txt")}})

    result = agent.handle({"op": "trash_contents", "params": {}})

    assert [item["name"] for item in result["files"]] == ["old.txt"]
    assert result["files"][0]["trashed"] is True


def test_emptying_the_trash_deletes_only_trashed_items(agent: AgentService, tmp_path: Path) -> None:
    agent.handle({"op": "trash", "params": {"id": _id(agent, "old.txt")}})

    result = agent.handle({"op": "empty_trash", "params": {"confirm": True}})

    assert result == {"ok": True, "deleted_count": 1}
    assert agent.handle({"op": "trash_contents", "params": {}})["files"] == []
    assert (tmp_path / "work" / "keep.txt").exists()


def test_emptying_needs_confirmation(agent: AgentService) -> None:
    agent.handle({"op": "trash", "params": {"id": _id(agent, "old.txt")}})

    result = agent.handle({"op": "empty_trash", "params": {"confirm": False}})

    assert result["ok"] is False
    assert len(agent.handle({"op": "trash_contents", "params": {}})["files"]) == 1
