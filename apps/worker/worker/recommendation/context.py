import uuid
from dataclasses import dataclass

from vault_shared.db.models import File, FileClassification, FileMetadata, FileRelationship


@dataclass(frozen=True)
class FileRow:
    """One organization-scoped file plus whatever the Knowledge/AI
    Intelligence Engines have already contributed for it — the
    Recommendation Engine's per-file unit of analysis. `metadata`/
    `classification` are `None` for a file that hasn't been enriched yet
    (itself a signal `PendingEnrichmentRule` reads directly)."""

    file: File
    metadata: FileMetadata | None
    classification: FileClassification | None
    workspace_domain: str | None


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule or insight generator needs, pulled once per
    `RecommendationJob` run and reused across every rule — Handbook's
    Recommendation Engine combining "metadata, knowledge graph, semantic
    search... deterministic rules" is realized here as one shared,
    organization-scoped data pull (see `FileRepository.
    list_for_organization_with_details`) rather than each rule running its
    own redundant query."""

    organization_id: uuid.UUID
    rows: list[FileRow]
    # Files this account owns — the only ones that use its storage and that
    # AI Vault can trash or archive. Storage rules read these; sharing and
    # security rules read every row.
    owned_rows: list[FileRow]
    relationships: list[FileRelationship]
    connector_count: int
    embedded_file_ids: set[uuid.UUID]


@dataclass(frozen=True)
class RuleResult:
    title: str
    description: str
    confidence: float
    estimated_impact: str
    impact_value: float | None
    risk_level: str
    suggested_action: str
    affected_file_ids: list[str]


@dataclass(frozen=True)
class InsightResult:
    title: str
    description: str
    confidence: float
    related_file_ids: list[str]
