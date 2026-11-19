"""Carrying out what the user confirmed in Ask Vault.

A proposal lives in the conversation's state until the user confirms it.
This service — deliberately outside the AI layer — re-checks every file id
against the organization, then performs each step through the same paths
the rest of the app uses: folders are created and confirmed by the provider
before anything moves into them, and every file change runs as an execution
plan in the Execution Engine, which verifies it against the provider. The
answer it records lists each step with the action id the client follows to
the provider-confirmed result.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.application.execution_plan_service import ExecutionPlanService
from app.application.storage_operation_service import StorageOperationService
from vault_shared import NotFoundError, VaultError
from vault_shared.db.models import ConversationMessage, ExecutionActionType, File, MessageRole
from vault_shared.db.repositories import (
    ConversationMessageRepository,
    ConversationRepository,
    FileRepository,
    FolderRepository,
)

_MAX_FILES_PER_PLAN = 500
_MAX_HISTORY = 20


def _chunks(ids: list[uuid.UUID]) -> list[list[uuid.UUID]]:
    return [ids[i : i + _MAX_FILES_PER_PLAN] for i in range(0, len(ids), _MAX_FILES_PER_PLAN)]


class VaultActionService:
    def __init__(
        self,
        db: Session,
        *,
        plans: ExecutionPlanService | None = None,
        operations: StorageOperationService | None = None,
    ) -> None:
        self._db = db
        self._plans = plans or ExecutionPlanService(db)
        self._operations = operations or StorageOperationService(db)
        self._conversations = ConversationRepository(db)
        self._messages = ConversationMessageRepository(db)
        self._files = FileRepository(db)
        self._folders = FolderRepository(db)

    # -- public ------------------------------------------------------------------------

    def confirm(
        self,
        conversation_id: uuid.UUID,
        action_id: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ConversationMessage:
        conversation, state, proposal = self._pending(
            conversation_id, action_id, organization_id=organization_id, user_id=user_id
        )
        self._org, self._user = organization_id, user_id
        steps: list[dict[str, Any]] = []
        handler = getattr(self, f"_do_{proposal['kind']}")
        handler(proposal, state, steps)

        pending = dict(state.get("pending") or {})
        pending.pop(action_id, None)
        entry = {
            "action_id": action_id,
            "title": proposal["title"],
            "steps": steps,
            "at": datetime.now(UTC).isoformat(),
        }
        state = {
            **state,
            "pending": pending,
            "history": [*(state.get("history") or []), entry][-_MAX_HISTORY:],
        }
        conversation.state = state
        self._mark_proposal(conversation.id, action_id, "confirmed")
        failed = [s for s in steps if s["status"] == "failed"]
        text = (
            "I couldn't do that — nothing was changed."
            if failed and len(failed) == len(steps)
            else "Working on it — each step below updates once the storage confirms it."
        )
        message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=text,
            tool_name="vault:action_result",
            blocks=[{"type": "action_result", **entry}],
        )
        self._conversations.touch(conversation)
        self._db.commit()
        return message

    def cancel(
        self,
        conversation_id: uuid.UUID,
        action_id: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ConversationMessage:
        conversation, state, _proposal = self._pending(
            conversation_id, action_id, organization_id=organization_id, user_id=user_id
        )
        pending = dict(state.get("pending") or {})
        pending.pop(action_id, None)
        conversation.state = {**state, "pending": pending}
        self._mark_proposal(conversation.id, action_id, "cancelled")
        message = self._messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Cancelled — nothing was changed.",
            tool_name="vault:action_cancelled",
        )
        self._conversations.touch(conversation)
        self._db.commit()
        return message

    # -- internals ---------------------------------------------------------------------

    def _pending(
        self,
        conversation_id: uuid.UUID,
        action_id: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        conversation = self._conversations.get_owned(
            conversation_id, organization_id=organization_id, user_id=user_id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        state = dict(conversation.state or {})
        proposal = (state.get("pending") or {}).get(action_id)
        if proposal is None:
            raise NotFoundError("That proposal was already handled or has expired.")
        return conversation, state, proposal

    def _mark_proposal(self, conversation_id: uuid.UUID, action_id: str, status: str) -> None:
        for message in self._messages.list_for_conversation(conversation_id):
            if any(block.get("action_id") == action_id for block in message.blocks or []):
                message.blocks = [
                    {**block, "status": status} if block.get("action_id") == action_id else block
                    for block in message.blocks
                ]

    def _owned(self, ids: list[str], *, trashed: bool = False) -> list[File]:
        parsed = [uuid.UUID(i) for i in ids]
        if trashed:
            return self._files.list_owned_by_organization_including_trashed(
                parsed, organization_id=self._org
            )
        return self._files.list_owned_by_organization(parsed, organization_id=self._org)

    def _by_storage(self, files: list[File]) -> dict[uuid.UUID, list[uuid.UUID]]:
        groups: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for file in files:
            groups[file.storage_source_id].append(file.id)
        return groups

    def _run_plans(
        self,
        steps: list[dict[str, Any]],
        file_ids: list[uuid.UUID],
        *,
        action_type: str,
        label: str,
        **kwargs: Any,
    ) -> None:
        for chunk in _chunks(file_ids):
            step_label = f"{label} ({len(chunk):,} file{'' if len(chunk) == 1 else 's'})"
            try:
                plan = self._plans.create_ad_hoc_plan(
                    chunk,
                    action_type=action_type,
                    organization_id=self._org,
                    user_id=self._user,
                    require_approval=False,
                    **kwargs,
                )
            except VaultError as error:
                steps.append({"label": step_label, "status": "failed", "message": str(error)})
                continue
            steps.append({"label": step_label, "status": "running", "plan_id": str(plan.id)})

    def _create_folder(
        self,
        steps: list[dict[str, Any]],
        connector_id: str,
        name: str,
        parent_folder_id: uuid.UUID | None = None,
    ) -> Any:
        try:
            created = self._operations.create_folder(
                uuid.UUID(connector_id),
                organization_id=self._org,
                user_id=self._user,
                name=name,
                parent_folder_id=parent_folder_id,
            )
        except VaultError as error:
            steps.append({"label": f"Create “{name}”", "status": "failed", "message": str(error)})
            return None
        steps.append(
            {
                "label": f"Create “{name}”",
                "status": "done",
                "message": f"Created {created.get('path') or name} — confirmed by the storage.",
            }
        )
        return self._folders.get_by_id(uuid.UUID(created["id"]))

    def _do_create_folder(self, proposal: dict, state: dict, steps: list) -> None:
        folder = self._create_folder(steps, proposal["connector_id"], proposal["name"])
        if folder is not None:
            state["last_folder"] = {
                "folder_id": str(folder.id),
                "connector_id": proposal["connector_id"],
                "name": folder.name,
            }

    def _do_move(self, proposal: dict, state: dict, steps: list) -> None:
        destination = proposal["destination"]
        if "create" in destination:
            folder = self._create_folder(steps, proposal["connector_id"], destination["create"])
        else:
            folder = self._folders.get_by_id(uuid.UUID(destination["folder_id"]))
        if folder is None:
            if not steps:
                steps.append(
                    {
                        "label": "Find the folder",
                        "status": "failed",
                        "message": "That folder is gone.",
                    }
                )
            return
        state["last_folder"] = {
            "folder_id": str(folder.id),
            "connector_id": proposal["connector_id"],
            "name": folder.name,
        }
        files = self._owned(proposal["file_ids"])
        self._run_plans(
            steps,
            [f.id for f in files],
            action_type=ExecutionActionType.MOVE_FILE,
            label=f"Move into “{folder.name}”",
            new_parent_id=folder.provider_file_id,
        )

    def _do_trash(self, proposal: dict, state: dict, steps: list) -> None:
        files = self._owned(proposal["file_ids"])
        for ids in self._by_storage(files).values():
            self._run_plans(
                steps, ids, action_type=ExecutionActionType.ARCHIVE, label="Move to Trash"
            )
        state["last_trashed"] = {"file_ids": [str(f.id) for f in files]}

    def _do_restore(self, proposal: dict, state: dict, steps: list) -> None:
        files = [f for f in self._owned(proposal["file_ids"], trashed=True) if f.trashed]
        if not files:
            steps.append(
                {
                    "label": "Restore",
                    "status": "failed",
                    "message": "None of them are in Trash any more.",
                }
            )
            return
        for ids in self._by_storage(files).values():
            self._run_plans(steps, ids, action_type=ExecutionActionType.RESTORE, label="Restore")

    def _do_archive(self, proposal: dict, state: dict, steps: list) -> None:
        files = self._owned(proposal["file_ids"])
        self._run_plans(
            steps,
            [f.id for f in files],
            action_type=ExecutionActionType.CREATE_ARCHIVE,
            label="Create archive",
        )

    def _do_rename(self, proposal: dict, state: dict, steps: list) -> None:
        owned = {str(f.id) for f in self._owned([r["file_id"] for r in proposal["renames"]])}
        for rename in proposal["renames"]:
            if rename["file_id"] not in owned:
                continue
            self._run_plans(
                steps,
                [uuid.UUID(rename["file_id"])],
                action_type=ExecutionActionType.RENAME,
                label=f"Rename to “{rename['new_name']}”",
                new_name=rename["new_name"],
            )

    def _do_organize(self, proposal: dict, state: dict, steps: list) -> None:
        root = self._create_folder(steps, proposal["connector_id"], proposal["root"])
        if root is None:
            return
        state["last_folder"] = {
            "folder_id": str(root.id),
            "connector_id": proposal["connector_id"],
            "name": root.name,
        }
        for planned in proposal["folders"]:
            folder = self._create_folder(steps, proposal["connector_id"], planned["name"], root.id)
            if folder is None:
                continue
            files = self._owned(planned["file_ids"])
            self._run_plans(
                steps,
                [f.id for f in files],
                action_type=ExecutionActionType.MOVE_FILE,
                label=f"Move into “{root.name}/{folder.name}”",
                new_parent_id=folder.provider_file_id,
            )
