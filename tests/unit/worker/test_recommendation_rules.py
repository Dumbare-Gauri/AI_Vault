import uuid
from datetime import UTC, datetime, timedelta

from vault_shared.db.models import (
    File,
    FileClassification,
    FileMetadata,
    FileRelationship,
)
from worker.recommendation.context import FileRow, RuleContext
from worker.recommendation.rules import (
    ArchiveCandidateRule,
    DuplicateFilesRule,
    InactiveSharedFilesRule,
    LargeUnusedFilesRule,
    LowConfidenceClassificationReviewRule,
    LowRelationshipCoverageRule,
    OrphanedOwnershipRule,
    OwnershipConcentrationRule,
    PendingEnrichmentRule,
    PubliclySharedFilesRule,
    SensitiveSharedContentRule,
)

_NOW = datetime.now(UTC)
_OLD = _NOW - timedelta(days=400)
_RECENT = _NOW - timedelta(days=5)


def _file(
    *,
    name: str = "File.txt",
    size_bytes: int | None = 1024,
    is_shared: bool = False,
    owner_email: str | None = "owner@acme.com",
    modified_at: datetime = _RECENT,
    viewed_at: datetime | None = None,
) -> File:
    return File(
        id=uuid.uuid4(),
        storage_source_id=uuid.uuid4(),
        provider_file_id=str(uuid.uuid4()),
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=size_bytes,
        owner_email=owner_email,
        is_shared=is_shared,
        provider_modified_at=modified_at,
        provider_viewed_at=viewed_at,
        scanned_at=_NOW,
    )


def _metadata(file: File, *, duplicate_group_key: str | None = None) -> FileMetadata:
    return FileMetadata(
        file_id=file.id, duplicate_group_key=duplicate_group_key, mime_type_validated=True, enriched_at=_NOW
    )


def _classification(
    file: File, *, document_type: str = "Documentation", confidence: float = 0.9
) -> FileClassification:
    return FileClassification(
        file_id=file.id, document_type=document_type, confidence=confidence, classified_at=_NOW
    )


def _row(
    file: File,
    *,
    metadata: FileMetadata | None = None,
    classification: FileClassification | None = None,
    workspace_domain: str | None = "acme.com",
) -> FileRow:
    return FileRow(
        file=file, metadata=metadata, classification=classification, workspace_domain=workspace_domain
    )


def _context(
    rows: list[FileRow],
    *,
    relationships: list[FileRelationship] | None = None,
    owner: str = "owner@acme.com",
) -> RuleContext:
    return RuleContext(
        organization_id=uuid.uuid4(),
        rows=rows,
        owned_rows=[row for row in rows if row.file.owner_email == owner],
        relationships=relationships or [],
        connector_count=1,
        embedded_file_ids=set(),
    )


def test_duplicate_files_rule_fires_for_a_shared_group_key() -> None:
    a = _file(name="A.pdf", size_bytes=100)
    b = _file(name="B.pdf", size_bytes=200)
    context = _context(
        [
            _row(a, metadata=_metadata(a, duplicate_group_key="grp-1")),
            _row(b, metadata=_metadata(b, duplicate_group_key="grp-1")),
        ]
    )

    result = DuplicateFilesRule().evaluate(context)

    assert result is not None
    # The smaller file (100 bytes) is the "redundant" one, larger kept.
    assert result.affected_file_ids == [str(a.id)]
    assert result.impact_value == 100.0


def test_duplicate_files_rule_does_not_fire_without_any_group() -> None:
    a = _file()
    context = _context([_row(a, metadata=_metadata(a))])

    assert DuplicateFilesRule().evaluate(context) is None


def test_archive_candidate_rule_fires_for_stale_files() -> None:
    old_file = _file(modified_at=_OLD)
    context = _context([_row(old_file)])

    result = ArchiveCandidateRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(old_file.id)]


def test_storage_rules_ignore_files_someone_else_owns() -> None:
    """A file shared with this account uses its owner's storage, not this
    account's — and AI Vault can't trash it."""
    shared_in = _file(modified_at=_OLD, size_bytes=500 * 1024 * 1024, owner_email="x@other.com")
    context = _context([_row(shared_in)])

    assert ArchiveCandidateRule().evaluate(context) is None
    assert LargeUnusedFilesRule().evaluate(context) is None


def test_archive_candidate_rule_does_not_fire_for_recent_files() -> None:
    context = _context([_row(_file(modified_at=_RECENT))])

    assert ArchiveCandidateRule().evaluate(context) is None


def test_large_unused_files_rule_requires_both_size_and_staleness() -> None:
    large_and_old = _file(size_bytes=200 * 1024 * 1024, modified_at=_OLD)
    large_but_recent = _file(size_bytes=200 * 1024 * 1024, modified_at=_RECENT)
    small_and_old = _file(size_bytes=10, modified_at=_OLD)
    context = _context([_row(large_and_old), _row(large_but_recent), _row(small_and_old)])

    result = LargeUnusedFilesRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(large_and_old.id)]


def test_pending_enrichment_rule_fires_for_files_with_no_metadata() -> None:
    unenriched = _file()
    enriched = _file()
    context = _context([_row(unenriched), _row(enriched, metadata=_metadata(enriched))])

    result = PendingEnrichmentRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(unenriched.id)]


def test_pending_enrichment_rule_does_not_fire_when_fully_enriched() -> None:
    f = _file()
    context = _context([_row(f, metadata=_metadata(f))])

    assert PendingEnrichmentRule().evaluate(context) is None


def test_low_relationship_coverage_rule_fires_when_almost_nothing_is_connected() -> None:
    rows = [_row(_file(), metadata=_metadata(_file())) for _ in range(12)]
    context = _context(rows, relationships=[])

    result = LowRelationshipCoverageRule().evaluate(context)

    assert result is not None


def test_low_relationship_coverage_rule_does_not_fire_below_the_file_count_floor() -> None:
    rows = [_row(_file(), metadata=_metadata(_file())) for _ in range(3)]
    context = _context(rows, relationships=[])

    assert LowRelationshipCoverageRule().evaluate(context) is None


def test_low_confidence_classification_review_rule_fires_for_low_confidence() -> None:
    uncertain = _file()
    confident = _file()
    context = _context(
        [
            _row(uncertain, classification=_classification(uncertain, confidence=0.2)),
            _row(confident, classification=_classification(confident, confidence=0.9)),
        ]
    )

    result = LowConfidenceClassificationReviewRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(uncertain.id)]


def test_publicly_shared_files_rule_fires_for_shared_files() -> None:
    shared = _file(is_shared=True)
    context = _context([_row(shared)])

    result = PubliclySharedFilesRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(shared.id)]


def test_publicly_shared_files_rule_does_not_fire_with_no_sharing() -> None:
    context = _context([_row(_file(is_shared=False))])

    assert PubliclySharedFilesRule().evaluate(context) is None


def test_orphaned_ownership_rule_fires_for_an_external_domain_owner() -> None:
    external = _file(owner_email="someone@othercorp.com")
    internal = _file(owner_email="person@acme.com")
    context = _context([_row(external), _row(internal)])

    result = OrphanedOwnershipRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(external.id)]


def test_orphaned_ownership_rule_does_not_fire_without_a_workspace_domain() -> None:
    external = _file(owner_email="someone@othercorp.com")
    context = _context([_row(external, workspace_domain=None)])

    assert OrphanedOwnershipRule().evaluate(context) is None


def test_sensitive_shared_content_rule_fires_for_shared_invoices() -> None:
    invoice = _file(is_shared=True)
    context = _context(
        [_row(invoice, classification=_classification(invoice, document_type="Invoice"))]
    )

    result = SensitiveSharedContentRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(invoice.id)]


def test_sensitive_shared_content_rule_does_not_fire_for_a_non_sensitive_type() -> None:
    shared_doc = _file(is_shared=True)
    context = _context(
        [_row(shared_doc, classification=_classification(shared_doc, document_type="Image"))]
    )

    assert SensitiveSharedContentRule().evaluate(context) is None


def test_inactive_shared_files_rule_requires_both_sharing_and_staleness() -> None:
    stale_shared = _file(is_shared=True, modified_at=_OLD)
    fresh_shared = _file(is_shared=True, modified_at=_RECENT)
    context = _context([_row(stale_shared), _row(fresh_shared)])

    result = InactiveSharedFilesRule().evaluate(context)

    assert result is not None
    assert result.affected_file_ids == [str(stale_shared.id)]


def test_ownership_concentration_rule_fires_when_one_owner_dominates() -> None:
    dominant_owner_files = [_file(is_shared=True, owner_email="alice@acme.com") for _ in range(8)]
    other_files = [_file(is_shared=True, owner_email=f"user{i}@acme.com") for i in range(2)]
    context = _context([_row(f) for f in [*dominant_owner_files, *other_files]])

    result = OwnershipConcentrationRule().evaluate(context)

    assert result is not None
    assert len(result.affected_file_ids) == 8


def test_ownership_concentration_rule_does_not_fire_below_the_file_count_floor() -> None:
    context = _context([_row(_file(is_shared=True)) for _ in range(3)])

    assert OwnershipConcentrationRule().evaluate(context) is None
