from celery.exceptions import TimeoutError as CeleryTimeoutError

from app.infrastructure.queue.celery_client import get_celery_client
from app.infrastructure.queue.correlation import correlation_headers
from vault_shared import DependencyUnavailableError

# Must match the task names registered by apps/worker/worker/tasks/execution.py.
CREATE_FOLDER_TASK = "worker.storage.create_folder"
CREATE_TEXT_FILE_TASK = "worker.storage.create_text_file"
PREVIEW_TRASH_TASK = "worker.storage.preview_trash"
MAKE_COPY_TASK = "worker.storage.make_copy"
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
