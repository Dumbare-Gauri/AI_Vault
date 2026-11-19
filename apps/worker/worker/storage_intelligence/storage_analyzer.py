import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from vault_shared.db.models import File
from vault_shared.db.repositories import (
    FolderRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.storage_intelligence.mime_category import classify_mime_category, size_bucket_for
from vault_shared.storage_intelligence.thresholds import SIZE_BUCKETS


@dataclass(frozen=True)
class StorageBreakdown:
    total_size_bytes: int
    total_files: int
    total_folders: int
    breakdown_by_type_bytes: dict[str, int] = field(default_factory=dict)
    breakdown_by_size_bucket_bytes: dict[str, int] = field(default_factory=dict)
    breakdown_by_source_bytes: dict[str, dict] = field(default_factory=dict)


class StorageAnalyzer:
    """ "Where is the user's storage going?" (Phase 1 spec §7) — a single
    O(n) pass over an already-fetched file list (see
    `FileRepository.list_all_for_organization`), aggregated in Python via
    `Counter`. Runs only inside the background `StorageAnalysisJob`, never
    on the HTTP request path — the same tradeoff the Recommendation
    Engine's `list_for_organization_with_details` already makes, and
    proven fast enough at reference scale by the earlier search-
    performance investigation (DB round-trips and Python-side aggregation
    over a few thousand rows cost low tens of milliseconds, not seconds)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._folders = FolderRepository(session)
        self._connectors = StorageConnectorRepository(session)
        self._sources = StorageSourceRepository(session)

    def analyze(
        self,
        organization_id: uuid.UUID,
        files: list[File],
        *,
        connector_id: uuid.UUID | None = None,
    ) -> StorageBreakdown:
        by_type: Counter[str] = Counter()
        by_bucket: Counter[str] = Counter()
        by_source_bytes: Counter[uuid.UUID] = Counter()
        total_size = 0

        for file in files:
            size = file.size_bytes or 0
            total_size += size
            by_type[classify_mime_category(file.mime_type)] += size
            by_bucket[size_bucket_for(file.size_bytes, SIZE_BUCKETS)] += size
            by_source_bytes[file.storage_source_id] += size

        source_names = self._source_names_for_organization(organization_id)
        breakdown_by_source = {
            str(source_id): {
                "name": source_names.get(source_id, "Unknown source"),
                "bytes": total,
            }
            for source_id, total in by_source_bytes.items()
        }

        return StorageBreakdown(
            total_size_bytes=total_size,
            total_files=len(files),
            total_folders=(
                self._folders.count_for_connector(connector_id)
                if connector_id is not None
                else self._folders.count_for_organization(organization_id)
            ),
            breakdown_by_type_bytes=dict(by_type),
            breakdown_by_size_bucket_bytes=dict(by_bucket),
            breakdown_by_source_bytes=breakdown_by_source,
        )

    def _source_names_for_organization(self, organization_id: uuid.UUID) -> dict[uuid.UUID, str]:
        names: dict[uuid.UUID, str] = {}
        for connector in self._connectors.list_for_organization(organization_id):
            for source in self._sources.list_for_connector(connector.id):
                names[source.id] = source.name
        return names
