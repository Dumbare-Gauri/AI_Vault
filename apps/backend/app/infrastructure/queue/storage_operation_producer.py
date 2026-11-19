from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.result import AsyncResult

from app.infrastructure.queue.celery_client import get_celery_client
from app.infrastructure.queue.correlation import correlation_headers
from vault_shared import DependencyUnavailableError

# Must match the task names registered by apps/worker/worker/tasks/execution.py.
CREATE_FOLDER_TASK = "worker.storage.create_folder"
CREATE_TEXT_FILE_TASK = "worker.storage.create_text_file"
PREVIEW_TRASH_TASK = "worker.storage.preview_trash"
MAKE_COPY_TASK = "worker.storage.make_copy"
EXTRACT_ARCHIVE_TASK = "worker.storage.extract_archive"
INSPECT_ARCHIVE_TASK = "worker.storage.inspect_archive"
EMPTY_TRASH_TASK = "worker.storage.empty_trash"

_WAIT_SECONDS = 60


def run_storage_operation(task_name: str, *, args: list, wait_seconds: int = _WAIT_SECONDS) -> dict:
    """Dispatches a single, quick provider mutation to the worker (the only
    process allowed to mutate storage) and waits for its provider-confirmed
    result, so the caller can report what actually happened rather than
    "started"."""
    pending = get_celery_client().send_task(task_name, args=args, headers=correlation_headers())
    try:
        result = pending.get(timeout=wait_seconds, disable_sync_subtasks=False)
    except CeleryTimeoutError as exc:
        raise DependencyUnavailableError(
            "Your storage provider is taking longer than expected. Check again shortly."
        ) from exc
    if not isinstance(result, dict):
        raise DependencyUnavailableError("The storage operation returned no result.")
    return result


def start_storage_operation(task_name: str, *, args: list) -> str:
    """Dispatches a long provider mutation without waiting; its result is
    read later with `storage_operation_result`."""
    pending = get_celery_client().send_task(task_name, args=args, headers=correlation_headers())
    return str(pending.id)


def storage_operation_result(task_id: str) -> dict | None:
    """The task's result once it has finished, or None while it runs. A
    task that crashed reports a failure, never success."""
    pending = AsyncResult(task_id, app=get_celery_client())
    if not pending.ready():
        return None
    if pending.failed():
        return {"ok": False, "error": "The operation failed in the worker."}
    result = pending.result
    return result if isinstance(result, dict) else {"ok": False, "error": "No result."}
