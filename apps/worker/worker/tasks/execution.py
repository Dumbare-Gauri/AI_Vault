import uuid

from sqlalchemy.orm import Session

from vault_shared import NotFoundError, ValidationError, VaultError
from vault_shared.connectors.google_workspace import get_google_workspace_oauth_client
from vault_shared.db.models import DriveType
from vault_shared.db.repositories import FolderRepository, StorageSourceRepository
from vault_shared.db.session import get_session_factory
from vault_shared.object_storage import ObjectStorageClient
from vault_shared.settings import get_settings
from vault_shared.storage.default_registry import build_storage_registry
from worker.celery_app import celery_app
from worker.execution.execution_service import ExecutionService, TrashSummary
from worker.organization.organization_recommendation_apply_service import (
    OrganizationRecommendationApplyService,
)


@celery_app.task(
    name="worker.execution.run", soft_time_limit=get_settings().execution_timeout_seconds
)
def run_execution(execution_job_id: str) -> None:
    """No `self.retry(...)` branch, unlike `worker.enrichment.run` — a
    failed execution *step* is already isolated per-file inside
    `ExecutionService` (never raises out to here), and a whole-job
    failure (e.g. the connector losing access mid-run) is deliberately
    terminal, not silently retried against real customer storage without
    a fresh human approval — the founder decides whether to try again via
    a new plan, not this task's own retry loop. `soft_time_limit` (the
    phase spec's `EXECUTION_TIMEOUT`) still bounds one run's wall-clock
    time, and Celery's `task_acks_late`/`task_reject_on_worker_lost`
    (`celery_app.py`) still redeliver on an actual worker-process crash —
    that's a different failure mode than a domain-level execution error."""
    session = get_session_factory()()
    try:
        service = ExecutionService(
            session,
            storage=build_storage_registry(
                session, oauth_client=get_google_workspace_oauth_client()
            ),
            object_storage_client=ObjectStorageClient(),
        )
        service.run(uuid.UUID(execution_job_id))
    finally:
        session.close()


@celery_app.task(name="worker.organization.apply_recommendation")
def run_apply_organization_recommendation(
    recommendation_id: str, *, organization_id: str, user_id: str
) -> None:
    """Phase 2 — dispatched by `POST /v1/organization-recommendations/{id}/
    apply`. Lives here, not `worker.tasks.organization`, because building
    the `StorageAdapterProvider` (`build_storage_registry`) this needs for
    `ExecutionService.create_folder()` is restricted to a small set of
    composition roots and this file already is one. A validation failure
    (recommendation not found/not active/stale) is a clean, expected
    outcome recorded in the log, not retried against real storage."""
    session = get_session_factory()()
    try:
        execution_service = ExecutionService(
            session,
            storage=build_storage_registry(
                session, oauth_client=get_google_workspace_oauth_client()
            ),
            object_storage_client=ObjectStorageClient(),
        )
        service = OrganizationRecommendationApplyService(
            session, execution_service=execution_service
        )
        service.apply(
            uuid.UUID(recommendation_id),
            organization_id=uuid.UUID(organization_id),
            user_id=uuid.UUID(user_id),
        )
    finally:
        session.close()


def _execution_service(session: Session) -> ExecutionService:
    return ExecutionService(
        session,
        storage=build_storage_registry(session, oauth_client=get_google_workspace_oauth_client()),
        object_storage_client=ObjectStorageClient(),
    )


def _resolve_source_id(
    session: Session, *, connector_id: uuid.UUID, parent_folder_id: uuid.UUID | None
) -> uuid.UUID:
    if parent_folder_id is not None:
        folder = FolderRepository(session).get_by_id(parent_folder_id)
        if folder is None:
            raise NotFoundError("Parent folder not found.")
        return folder.storage_source_id
    sources = StorageSourceRepository(session).list_for_connector(connector_id)
    primary = [s for s in sources if s.drive_type in (DriveType.MY_DRIVE, DriveType.LOCAL_FOLDER)]
    if not primary:
        raise NotFoundError("This connection hasn't been scanned yet — scan it first.")
    if len(primary) > 1:
        raise ValidationError("Choose which folder to create it in.")
    return primary[0].id


@celery_app.task(name="worker.storage.create_folder")
def create_folder(
    organization_id: str, connector_id: str, name: str, parent_folder_id: str | None
) -> dict:
    """Request/response task for the API's "New folder": returns the created,
    provider-confirmed folder, or a user-facing error — never a fake
    success."""
    session = get_session_factory()()
    try:
        parent = uuid.UUID(parent_folder_id) if parent_folder_id else None
        folder = _execution_service(session).create_folder(
            organization_id=uuid.UUID(organization_id),
            storage_source_id=_resolve_source_id(
                session, connector_id=uuid.UUID(connector_id), parent_folder_id=parent
            ),
            name=name,
            parent_folder_id=parent,
        )
        return {
            "ok": True,
            "id": str(folder.id),
            "name": folder.name,
            "path": folder.path,
            "provider_file_id": folder.provider_file_id,
        }
    except VaultError as exc:
        return {"ok": False, "error": exc.message}
    finally:
        session.close()


@celery_app.task(name="worker.storage.create_text_file")
def create_text_file(
    organization_id: str,
    connector_id: str,
    name: str,
    content: str,
    mime_type: str,
    parent_folder_id: str | None,
) -> dict:
    session = get_session_factory()()
    try:
        parent = uuid.UUID(parent_folder_id) if parent_folder_id else None
        file = _execution_service(session).create_text_file(
            organization_id=uuid.UUID(organization_id),
            storage_source_id=_resolve_source_id(
                session, connector_id=uuid.UUID(connector_id), parent_folder_id=parent
            ),
            name=name,
            content=content,
            mime_type=mime_type,
            parent_folder_id=parent,
        )
        return {
            "ok": True,
            "id": str(file.id),
            "name": file.name,
            "path": file.path,
            "web_view_link": file.web_view_link,
        }
    except VaultError as exc:
        return {"ok": False, "error": exc.message}
    finally:
        session.close()


@celery_app.task(name="worker.storage.make_copy")
def make_copy(organization_id: str, file_id: str, new_name: str | None) -> dict:
    session = get_session_factory()()
    try:
        file = _execution_service(session).make_copy(
            organization_id=uuid.UUID(organization_id),
            file_id=uuid.UUID(file_id),
            new_name=new_name,
        )
        return {
            "ok": True,
            "id": str(file.id),
            "name": file.name,
            "path": file.path,
            "web_view_link": file.web_view_link,
        }
    except VaultError as exc:
        return {"ok": False, "error": exc.message}
    finally:
        session.close()


def _trash_payload(summary: TrashSummary) -> dict:
    return {
        "ok": True,
        "file_count": summary.file_count,
        "total_bytes": summary.total_bytes,
        "not_backed_up_count": summary.not_backed_up_count,
        "still_deleting_count": summary.still_deleting_count,
        "largest": [
            {"name": item.name, "size_bytes": item.size_bytes, "backed_up": item.backed_up}
            for item in summary.largest
        ],
    }


@celery_app.task(name="worker.storage.preview_trash")
def preview_trash(organization_id: str, connector_id: str) -> dict:
    session = get_session_factory()()
    try:
        summary = _execution_service(session).preview_trash(
            organization_id=uuid.UUID(organization_id), connector_id=uuid.UUID(connector_id)
        )
        return _trash_payload(summary)
    except VaultError as exc:
        return {"ok": False, "error": exc.message}
    finally:
        session.close()


@celery_app.task(name="worker.storage.empty_trash")
def empty_trash(organization_id: str, connector_id: str, expected_count: int) -> dict:
    """Permanently deletes the provider's Trash and reports what the provider
    confirmed is gone."""
    session = get_session_factory()()
    try:
        summary = _execution_service(session).empty_trash(
            organization_id=uuid.UUID(organization_id),
            connector_id=uuid.UUID(connector_id),
            expected_count=expected_count,
        )
        return _trash_payload(summary)
    except VaultError as exc:
        return {"ok": False, "error": exc.message}
    finally:
        session.close()
