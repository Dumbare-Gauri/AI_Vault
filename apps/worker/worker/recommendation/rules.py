from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vault_shared.db.models import File, RecommendationCategory, RecommendationRiskLevel
from vault_shared.formatting import human_bytes
from worker.recommendation.context import FileRow, RuleContext, RuleResult

_ARCHIVE_STALE_DAYS = 365
_LARGE_UNUSED_STALE_DAYS = 180
_LARGE_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
_LOW_CONFIDENCE_THRESHOLD = 0.4
_LOW_RELATIONSHIP_COVERAGE_RATIO = 0.10
_OWNERSHIP_CONCENTRATION_RATIO = 0.5
_MIN_FILES_FOR_CONCENTRATION_CHECK = 10
# The mime/extension-based classifier's own sensitive-leaning types (see
# `FileTypeClassifierProcessor` — Phase 5). Not a real content-sensitivity
# scan (this platform has no such signal); an explicit, documented proxy.
_SENSITIVE_DOCUMENT_TYPES = frozenset({"Invoice", "Contract"})


def _last_activity(file: File) -> datetime | None:
    candidates = [ts for ts in (file.provider_modified_at, file.provider_viewed_at) if ts]
    return max(candidates) if candidates else None


def _is_stale(file: File, *, days: int) -> bool:
    last_activity = _last_activity(file)
    if last_activity is None:
        return False
    return last_activity < datetime.now(UTC) - timedelta(days=days)


class RecommendationRule(Protocol):
    name: str
    category: str

    def evaluate(self, context: RuleContext) -> RuleResult | None: ...


class DuplicateFilesRule:
    """Reuses Phase 5's checksum-exact `duplicate_group_key` — this rule
    adds no new duplicate detection, it just aggregates an already-
    deterministic, already-explainable signal into one storage-savings
    recommendation."""

    name = "duplicate_files"
    category = RecommendationCategory.STORAGE_OPTIMIZATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        groups: dict[str, list] = defaultdict(list)
        for row in context.owned_rows:
            if row.metadata and row.metadata.duplicate_group_key:
                groups[row.metadata.duplicate_group_key].append(row)

        redundant_files: list = []
        reclaimable_bytes = 0
        for rows in groups.values():
            if len(rows) < 2:
                continue
            ordered = sorted(rows, key=lambda r: r.file.size_bytes or 0, reverse=True)
            for row in ordered[1:]:
                redundant_files.append(row)
                reclaimable_bytes += row.file.size_bytes or 0

        if not redundant_files:
            return None

        group_count = sum(1 for rows in groups.values() if len(rows) >= 2)
        return RuleResult(
            title=f"{group_count} duplicate file group{'s' if group_count != 1 else ''} found",
            description=(
                f"{len(redundant_files)} files across {group_count} groups share an identical "
                "checksum with another file already in your storage — exact-content duplicates, "
                "not just similarly-named files."
            ),
            confidence=0.9,
            estimated_impact=f"~{human_bytes(reclaimable_bytes)} could be reclaimed",
            impact_value=float(reclaimable_bytes),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action=(
                "Review each duplicate group and remove the redundant copies, keeping the "
                "most recently modified file in each group."
            ),
            affected_file_ids=[str(row.file.id) for row in redundant_files],
        )


class ArchiveCandidateRule:
    name = "archive_candidates"
    category = RecommendationCategory.STORAGE_OPTIMIZATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        stale = [row for row in context.owned_rows if _is_stale(row.file, days=_ARCHIVE_STALE_DAYS)]
        if not stale:
            return None

        total_bytes = sum(row.file.size_bytes or 0 for row in stale)
        return RuleResult(
            title=f"{len(stale)} files haven't been touched in over a year",
            description=(
                f"{len(stale)} files have not been modified or viewed in more than "
                f"{_ARCHIVE_STALE_DAYS} days — candidates for archiving rather than active storage."
            ),
            confidence=0.6,
            estimated_impact=f"~{human_bytes(total_bytes)} in inactive storage",
            impact_value=float(total_bytes),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action="Consider archiving or removing files with no recent activity.",
            affected_file_ids=[str(row.file.id) for row in stale],
        )


class LargeUnusedFilesRule:
    name = "large_unused_files"
    category = RecommendationCategory.STORAGE_OPTIMIZATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        candidates = [
            row
            for row in context.owned_rows
            if (row.file.size_bytes or 0) > _LARGE_FILE_BYTES
            and _is_stale(row.file, days=_LARGE_UNUSED_STALE_DAYS)
        ]
        if not candidates:
            return None

        total_bytes = sum(row.file.size_bytes or 0 for row in candidates)
        return RuleResult(
            title=f"{len(candidates)} large files with no recent activity",
            description=(
                f"{len(candidates)} files over {human_bytes(_LARGE_FILE_BYTES)} each haven't been "
                f"modified or viewed in over {_LARGE_UNUSED_STALE_DAYS} days."
            ),
            confidence=0.65,
            estimated_impact=f"~{human_bytes(total_bytes)} in large, unused files",
            impact_value=float(total_bytes),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action="Review these large files and remove or archive any no longer needed.",
            affected_file_ids=[str(row.file.id) for row in candidates],
        )


class PendingEnrichmentRule:
    """Files never touched by the Knowledge Engine at all (Phase 5 spec's
    "Documents pending enrichment" — the same "pending" concept as
    `FileRepository.list_pending_enrichment_for_connector`, just surfaced
    here as a dashboard-visible backlog rather than an internal worker
    query)."""

    name = "pending_enrichment"
    category = RecommendationCategory.KNOWLEDGE_OPTIMIZATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        pending = [row for row in context.rows if row.metadata is None]
        if not pending:
            return None

        ratio = len(pending) / len(context.rows) if context.rows else 0.0
        return RuleResult(
            title=f"{len(pending)} files pending knowledge processing",
            description=(
                f"{len(pending)} files ({ratio:.0%} of your storage) have not yet been "
                "processed by the Knowledge Engine, so they have no classification, extracted "
                "content, or discovered relationships."
            ),
            confidence=0.95,
            estimated_impact=f"{len(pending)} files with no knowledge coverage",
            impact_value=float(len(pending)),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action="Run enrichment for the affected connector(s) to process these files.",
            affected_file_ids=[str(row.file.id) for row in pending],
        )


class LowRelationshipCoverageRule:
    name = "low_relationship_coverage"
    category = RecommendationCategory.KNOWLEDGE_OPTIMIZATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        enriched = [row for row in context.rows if row.metadata is not None]
        if len(enriched) < _MIN_FILES_FOR_CONCENTRATION_CHECK:
            return None

        connected_ids = set()
        for relationship in context.relationships:
            connected_ids.add(relationship.file_id)
            connected_ids.add(relationship.related_file_id)

        covered = sum(1 for row in enriched if row.file.id in connected_ids)
        coverage_ratio = covered / len(enriched)
        if coverage_ratio >= _LOW_RELATIONSHIP_COVERAGE_RATIO:
            return None

        return RuleResult(
            title="Low relationship coverage across your knowledge graph",
            description=(
                f"Only {coverage_ratio:.0%} of enriched files participate in any discovered "
                "relationship (version, duplicate, or shared-ownership) — most files are "
                "knowledge-isolated, with no connections helping you navigate related work."
            ),
            confidence=0.5,
            estimated_impact=f"{len(enriched) - covered} files with no known relationships",
            impact_value=float(len(enriched) - covered),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action=(
                "Re-run enrichment after organizing files into clearer folder/naming "
                "conventions to help relationship discovery find more connections."
            ),
            affected_file_ids=[],
        )


class LowConfidenceClassificationReviewRule:
    name = "low_confidence_classification_review"
    category = RecommendationCategory.PRODUCTIVITY

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        uncertain = [
            row
            for row in context.rows
            if row.classification and row.classification.confidence < _LOW_CONFIDENCE_THRESHOLD
        ]
        if not uncertain:
            return None

        return RuleResult(
            title=f"{len(uncertain)} documents classified with low confidence",
            description=(
                f"{len(uncertain)} files were auto-classified with confidence below "
                f"{_LOW_CONFIDENCE_THRESHOLD:.0%} and may be miscategorized — worth a manual look."
            ),
            confidence=0.55,
            estimated_impact=f"{len(uncertain)} documents requiring review",
            impact_value=float(len(uncertain)),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action="Manually review these files' classification and correct if needed.",
            affected_file_ids=[str(row.file.id) for row in uncertain],
        )


class PubliclySharedFilesRule:
    """`File.is_shared` is a binary flag (Phase 3/4's scanner never
    captures Drive's actual permission granularity — "anyone with the
    link" vs "shared with specific people" are indistinguishable today) —
    this rule is an explicit, documented approximation, not a precise
    public-exposure scan."""

    name = "publicly_shared_files"
    category = RecommendationCategory.SECURITY

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        shared = [row for row in context.rows if row.file.is_shared]
        if not shared:
            return None

        return RuleResult(
            title=f"{len(shared)} files are shared",
            description=(
                f"{len(shared)} files are marked as shared. This platform currently tracks "
                "whether a file is shared, not the precise audience (link-shared vs specific "
                "people) — review sharing settings directly in Google Drive for exact scope."
            ),
            confidence=0.5,
            estimated_impact=f"{len(shared)} files with active sharing",
            impact_value=float(len(shared)),
            risk_level=RecommendationRiskLevel.MEDIUM,
            suggested_action=(
                "Review sharing settings for these files to confirm access is still appropriate."
            ),
            affected_file_ids=[str(row.file.id) for row in shared],
        )


class OrphanedOwnershipRule:
    name = "orphaned_ownership"
    category = RecommendationCategory.SECURITY

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        orphaned = [row for row in context.rows if self._is_external_owner(row)]
        if not orphaned:
            return None

        return RuleResult(
            title=f"{len(orphaned)} files owned outside your workspace domain",
            description=(
                f"{len(orphaned)} files are owned by an account outside your workspace's "
                "domain — if that person leaves or loses access, these files could become "
                "inaccessible or ownerless."
            ),
            confidence=0.85,
            estimated_impact=f"{len(orphaned)} files at ownership risk",
            impact_value=float(len(orphaned)),
            risk_level=RecommendationRiskLevel.HIGH,
            suggested_action=(
                "Verify these files should be owned externally, or transfer ownership to a "
                "workspace member."
            ),
            affected_file_ids=[str(row.file.id) for row in orphaned],
        )

    @staticmethod
    def _is_external_owner(row: FileRow) -> bool:
        if not row.file.owner_email or not row.workspace_domain:
            return False
        owner_domain = row.file.owner_email.rsplit("@", 1)[-1].lower()
        return owner_domain != row.workspace_domain.lower()


class SensitiveSharedContentRule:
    name = "sensitive_shared_content"
    category = RecommendationCategory.SECURITY

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        exposed = [
            row
            for row in context.rows
            if row.file.is_shared
            and row.classification
            and row.classification.document_type in _SENSITIVE_DOCUMENT_TYPES
        ]
        if not exposed:
            return None

        return RuleResult(
            title=f"{len(exposed)} sensitive-looking documents are shared",
            description=(
                f"{len(exposed)} files classified as Invoice or Contract are also marked as "
                "shared — worth confirming sharing is intentional given their likely sensitivity."
            ),
            confidence=0.6,
            estimated_impact=f"{len(exposed)} sensitive documents exposed via sharing",
            impact_value=float(len(exposed)),
            risk_level=RecommendationRiskLevel.HIGH,
            suggested_action=(
                "Review sharing permissions on these documents given their classification."
            ),
            affected_file_ids=[str(row.file.id) for row in exposed],
        )


class InactiveSharedFilesRule:
    name = "inactive_shared_files"
    category = RecommendationCategory.COLLABORATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        inactive = [
            row
            for row in context.rows
            if row.file.is_shared and _is_stale(row.file, days=_ARCHIVE_STALE_DAYS)
        ]
        if not inactive:
            return None

        return RuleResult(
            title=f"{len(inactive)} shared files show no recent activity",
            description=(
                f"{len(inactive)} shared files have had no activity in over "
                f"{_ARCHIVE_STALE_DAYS} days — likely stale collaboration, not active teamwork."
            ),
            confidence=0.55,
            estimated_impact=f"{len(inactive)} inactive shared files",
            impact_value=float(len(inactive)),
            risk_level=RecommendationRiskLevel.LOW,
            suggested_action=(
                "Confirm these shared files are still needed, or revoke stale sharing."
            ),
            affected_file_ids=[str(row.file.id) for row in inactive],
        )


class OwnershipConcentrationRule:
    name = "ownership_concentration"
    category = RecommendationCategory.COLLABORATION

    def evaluate(self, context: RuleContext) -> RuleResult | None:
        shared = [row for row in context.rows if row.file.is_shared and row.file.owner_email]
        if len(shared) < _MIN_FILES_FOR_CONCENTRATION_CHECK:
            return None

        counts: dict[str, int] = defaultdict(int)
        for row in shared:
            counts[row.file.owner_email] += 1
        top_owner, top_count = max(counts.items(), key=lambda item: item[1])
        ratio = top_count / len(shared)
        if ratio < _OWNERSHIP_CONCENTRATION_RATIO:
            return None

        affected = [str(row.file.id) for row in shared if row.file.owner_email == top_owner]
        return RuleResult(
            title=f"One person owns {ratio:.0%} of shared files",
            description=(
                f"{top_owner} owns {top_count} of {len(shared)} shared files ({ratio:.0%}) — a "
                "potential single point of failure if that person becomes unavailable."
            ),
            confidence=0.6,
            estimated_impact=f"{top_count} files concentrated with one owner",
            impact_value=float(top_count),
            risk_level=RecommendationRiskLevel.MEDIUM,
            suggested_action=(
                "Consider distributing ownership or adding co-owners for critical files."
            ),
            affected_file_ids=affected,
        )


RULES: list[RecommendationRule] = [
    DuplicateFilesRule(),
    ArchiveCandidateRule(),
    LargeUnusedFilesRule(),
    PendingEnrichmentRule(),
    LowRelationshipCoverageRule(),
    PubliclySharedFilesRule(),
    OrphanedOwnershipRule(),
    SensitiveSharedContentRule(),
    InactiveSharedFilesRule(),
    OwnershipConcentrationRule(),
    LowConfidenceClassificationReviewRule(),
]
