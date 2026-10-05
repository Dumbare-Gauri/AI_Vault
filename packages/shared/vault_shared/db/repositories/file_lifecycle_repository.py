import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vault_shared.db.models import File, FileLifecycle, StorageConnector, StorageSource


def _organization_files(organization_id: uuid.UUID):
    return (
        select(File.id)
        .join(StorageSource, StorageSource.id == File.storage_source_id)
        .join(StorageConnector, StorageConnector.id == StorageSource.connector_id)
        .where(
            StorageConnector.organization_id == organization_id,
            File.trashed.is_(False),
            File.permanently_deleted_at.is_(None),
        )
    )


class FileLifecycleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_file_id(self, file_id: uuid.UUID) -> FileLifecycle | None:
        return self._session.get(FileLifecycle, file_id)

    def upsert(
        self,
        *,
        file_id: uuid.UUID,
        state: str,
        confidence: float,
        evidence: list[str],
        signals: dict,
        scorer_version: str,
        analyzed_at: datetime,
    ) -> FileLifecycle:
        lifecycle = self.get_by_file_id(file_id)
        if lifecycle is None:
            lifecycle = FileLifecycle(file_id=file_id)
            self._session.add(lifecycle)

        lifecycle.state = state
        lifecycle.confidence = confidence
        lifecycle.evidence = evidence
        lifecycle.signals = signals
        lifecycle.scorer_version = scorer_version
        lifecycle.analyzed_at = analyzed_at
        self._session.flush()
        return lifecycle

    def count_states_for_organization(self, organization_id: uuid.UUID) -> dict[str, int]:
        rows = self._session.execute(
            select(FileLifecycle.state, func.count())
            .where(FileLifecycle.file_id.in_(_organization_files(organization_id)))
            .group_by(FileLifecycle.state)
        ).all()
        return {state: count for state, count in rows}
