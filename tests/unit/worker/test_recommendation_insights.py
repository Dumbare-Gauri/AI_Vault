import uuid
from datetime import UTC, datetime

from vault_shared.db.models import File, FileClassification, FileRelationship
from worker.recommendation.context import FileRow, RuleContext
from worker.recommendation.insights import (
    FileSizeAnomalyInsight,
    HighConnectivityDocumentsInsight,
)

_NOW = datetime.now(UTC)


def _file(*, name: str = "File.txt", size_bytes: int = 1024) -> File:
    return File(
        id=uuid.uuid4(),
        storage_source_id=uuid.uuid4(),
        provider_file_id=str(uuid.uuid4()),
        name=name,
        path=f"/{name}",
        mime_type="text/plain",
        size_bytes=size_bytes,
        is_shared=False,
        scanned_at=_NOW,
    )


def _classification(file: File, *, document_type: str = "Documentation") -> FileClassification:
    return FileClassification(
        file_id=file.id, document_type=document_type, confidence=0.9, classified_at=_NOW
    )


def _relationship(file_id: uuid.UUID, related_file_id: uuid.UUID) -> FileRelationship:
    return FileRelationship(
        connector_id=uuid.uuid4(),
        file_id=file_id,
        related_file_id=related_file_id,
        relationship_type="duplicate_candidate",
        confidence=0.9,
        metadata_={},
        discovered_at=_NOW,
    )


def _context(rows: list[FileRow], relationships: list[FileRelationship]) -> RuleContext:
    return RuleContext(
        organization_id=uuid.uuid4(),
        rows=rows,
        owned_rows=rows,
        relationships=relationships,
        connector_count=1,
        embedded_file_ids=set(),
    )


def test_high_connectivity_insight_fires_for_a_well_connected_file() -> None:
    hub = _file(name="Hub.pdf")
    leaf_a = _file(name="LeafA.pdf")
    leaf_b = _file(name="LeafB.pdf")
    rows = [
        FileRow(file=hub, metadata=None, classification=None, workspace_domain=None),
        FileRow(file=leaf_a, metadata=None, classification=None, workspace_domain=None),
        FileRow(file=leaf_b, metadata=None, classification=None, workspace_domain=None),
    ]
    relationships = [_relationship(hub.id, leaf_a.id), _relationship(hub.id, leaf_b.id)]

    result = HighConnectivityDocumentsInsight().evaluate(_context(rows, relationships))

    assert result is not None
    assert str(hub.id) in result.related_file_ids


def test_high_connectivity_insight_does_not_fire_with_no_relationships() -> None:
    rows = [FileRow(file=_file(), metadata=None, classification=None, workspace_domain=None)]

    assert HighConnectivityDocumentsInsight().evaluate(_context(rows, [])) is None


def test_file_size_anomaly_insight_fires_for_an_outsized_file() -> None:
    typical = [_file(size_bytes=1000) for _ in range(6)]
    outlier = _file(size_bytes=1000 * 50)
    rows = [
        FileRow(file=f, metadata=None, classification=_classification(f), workspace_domain=None)
        for f in [*typical, outlier]
    ]

    result = FileSizeAnomalyInsight().evaluate(_context(rows, []))

    assert result is not None
    assert str(outlier.id) in result.related_file_ids


def test_file_size_anomaly_insight_does_not_fire_below_the_group_size_floor() -> None:
    files = [_file(size_bytes=1000), _file(size_bytes=50000)]
    rows = [
        FileRow(file=f, metadata=None, classification=_classification(f), workspace_domain=None)
        for f in files
    ]

    assert FileSizeAnomalyInsight().evaluate(_context(rows, [])) is None
