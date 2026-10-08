"""The structured parts of an Ask Vault answer.

An answer is text plus blocks the client renders as real interface — file
cards, duplicate groups, storage summaries, proposed actions and their
results. Lists carry their full id set server-side (`file_ids`/`group_ids`)
so the client can page through every result; only the first page travels
with the message.
"""

import uuid
from collections.abc import Callable
from typing import Any

from vault_shared.db.models import File, StorageConnector

FIRST_PAGE = 25
MAX_RESULT_IDS = 5000
# Never sent to the client: the client pages through these via the API.
INTERNAL_KEYS = frozenset({"file_ids", "group_ids"})


def file_item(file: File, connector: StorageConnector | None, label: str | None) -> dict[str, Any]:
    return {
        "id": str(file.id),
        "name": file.name,
        "path": file.path,
        "mime_type": file.mime_type,
        "size_bytes": file.size_bytes,
        "modified_at": file.provider_modified_at.isoformat() if file.provider_modified_at else None,
        "storage": label,
        "connector_id": str(connector.id) if connector else None,
        "trashed": file.trashed,
    }


ItemFor = Callable[[File], dict[str, Any]]


def file_list(
    title: str, files: list[File], total: int, item_for: ItemFor, *, note: str | None = None
) -> dict[str, Any]:
    ids = [str(file.id) for file in files[:MAX_RESULT_IDS]]
    return {
        "type": "file_list",
        "title": title,
        "total": total,
        "items": [item_for(file) for file in files[:FIRST_PAGE]],
        "file_ids": ids,
        "note": note,
    }


def duplicate_groups(
    title: str,
    groups: list[dict[str, Any]],
    *,
    total_groups: int,
    recoverable_bytes: int,
    group_ids: list[uuid.UUID],
) -> dict[str, Any]:
    return {
        "type": "duplicate_groups",
        "title": title,
        "total_groups": total_groups,
        "recoverable_bytes": recoverable_bytes,
        "groups": groups[:FIRST_PAGE],
        "group_ids": [str(group_id) for group_id in group_ids[:MAX_RESULT_IDS]],
    }


def notice(text: str, *, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def public(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks as the client receives them — without the server-side id sets,
    but with how many results there are to page through."""
    return [{k: v for k, v in block.items() if k not in INTERNAL_KEYS} for block in blocks]
