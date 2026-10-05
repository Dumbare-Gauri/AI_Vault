import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_archive_service, get_file_service
from vault_shared import NotFoundError

client = TestClient(app)


class _FakeFile:
    def __init__(self, *, name: str = "Report.pdf") -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.path = f"/Finance/{name}"
        self.mime_type = "application/pdf"
        self.size_bytes = 2048
        self.is_shared = False
        self.owner_email = "founder@acme.com"
        self.provider_modified_at = datetime.now(UTC)
        self.web_view_link = f"https://drive.google.com/file/d/{self.id}/view"


class _FakeFileMetadata:
    def __init__(self) -> None:
        self.normalized_extension = "pdf"
        self.mime_type_validated = True
        self.mime_mismatch_reason = None
        self.naming_pattern = "plain"
        self.version_label = None
        self.owner_summary = "founder@acme.com"
        self.sharing_summary = "private"
        self.duplicate_group_key = None
        self.language = "en"
        self.enriched_at = datetime.now(UTC)


class _FakeFileClassification:
    def __init__(self) -> None:
        self.document_type = "Documentation"
        self.confidence = 0.5
        self.method = "mime_document_default"
        self.classified_at = datetime.now(UTC)


class _FakeFileExtraction:
    def __init__(self) -> None:
        self.status = "success"
        self.extractor_name = "pdf"
        self.char_count = 120
        self.error = None
        self.extracted_at = datetime.now(UTC)


class _FakeFileIntelligence:
    def __init__(self) -> None:
        self.status = "success"
        self.document_type = "contract"
        self.summary = "A services agreement between two parties."
        self.entities = [{"type": "party", "value": "Acme Corp", "confidence": 0.9}]
        self.structured_metadata = {"effective_date": "2026-01-01"}
        self.topics = ["services"]
        self.confidence = 0.87
        self.provider = "openai_compatible"
        self.model_name = "kimi-k2-0711-preview"
        self.error = None
        self.processed_at = datetime.now(UTC)


class _FakeKnowledgeAttribute:
    def __init__(self, *, attribute_type: str, value: str) -> None:
        self.attribute_type = attribute_type
        self.value = value
        self.confidence = 0.6
        self.source = "folder_context_analyzer"


@dataclass
class _FakeFileRelationshipRow:
    file_id: uuid.UUID
    related_file_id: uuid.UUID
    relationship_type: str = "sequential_version"
    confidence: float = 0.85
    metadata_: dict = field(default_factory=dict)


@dataclass
class _FakeRelatedFile:
    file: _FakeFile
    relationship: _FakeFileRelationshipRow


class _FakeOrganizationEntity:
    def __init__(self, *, entity_type: str = "project", name: str = "Phoenix") -> None:
        self.id = uuid.uuid4()
        self.entity_type = entity_type
        self.name = name


@dataclass
class _FakeFileEntityLink:
    entity_id: uuid.UUID
    confidence: float = 0.8
    evidence: list = field(default_factory=list)
    is_user_confirmed: bool = False
    source: str = "ai_inferred"


@dataclass
class _FakeLinkedEntity:
    link: _FakeFileEntityLink
    entity: _FakeOrganizationEntity


class _FakeFileLifecycle:
    def __init__(self) -> None:
        self.state = "active"
        self.confidence = 0.8
        self.evidence = ["Linked to an active project and modified recently."]
        self.analyzed_at = datetime.now(UTC)


@dataclass
class _FakeFileDetail:
    file: _FakeFile
    metadata: _FakeFileMetadata | None
    classification: _FakeFileClassification | None
    extraction: _FakeFileExtraction | None
    intelligence: _FakeFileIntelligence | None
    knowledge_attributes: list
    related_files: list
    entity_links: list = field(default_factory=list)
    lifecycle: _FakeFileLifecycle | None = None


@pytest.fixture
def fake_file_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_file_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_file_service, None)


@pytest.fixture(autouse=True)
def fake_archive_service() -> MagicMock:
    """`list_trashed_files` also depends on `ArchiveService` (to flag which
    trashed files are already backed up) — autouse since every test hitting
    that endpoint needs it overridden, and a real default (empty set, not
    a bare MagicMock) so `file.id in archived_ids` never blows up."""
    service = MagicMock()
    service.list_archived_file_ids.return_value = set()
    app.dependency_overrides[get_archive_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_archive_service, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_list_files_requires_authentication(fake_file_service) -> None:
    response = client.get(f"/v1/connectors/{uuid.uuid4()}/files")
    assert response.status_code == 401


def test_list_files_returns_a_paginated_envelope(as_member, fake_file_service) -> None:
    fake_file_service.list_for_connector.return_value = ([_FakeFile()], 1)

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/files")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Report.pdf"


def test_list_files_returns_not_found_for_another_organizations_connector(
    as_member, fake_file_service
) -> None:
    fake_file_service.list_for_connector.side_effect = NotFoundError("Connector not found.")

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/files")

    assert response.status_code == 404


def test_list_files_passes_the_ownership_filter_through(as_member, fake_file_service) -> None:
    fake_file_service.list_for_connector.return_value = ([], 0)

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/files?ownership=shared")

    assert response.status_code == 200
    assert fake_file_service.list_for_connector.call_args.kwargs["ownership"] == "shared"


def test_list_files_rejects_an_invalid_ownership_value(as_member, fake_file_service) -> None:
    response = client.get(f"/v1/connectors/{uuid.uuid4()}/files?ownership=nonsense")

    assert response.status_code == 422


def test_get_file_detail_requires_authentication(fake_file_service) -> None:
    response = client.get(f"/v1/files/{uuid.uuid4()}")
    assert response.status_code == 401


def test_get_file_detail_returns_the_full_enrichment_picture(as_member, fake_file_service) -> None:
    file = _FakeFile()
    related_file = _FakeFile(name="Report_v1.pdf")
    entity = _FakeOrganizationEntity()
    detail = _FakeFileDetail(
        file=file,
        metadata=_FakeFileMetadata(),
        classification=_FakeFileClassification(),
        extraction=_FakeFileExtraction(),
        intelligence=_FakeFileIntelligence(),
        knowledge_attributes=[
            _FakeKnowledgeAttribute(attribute_type="department", value="Finance")
        ],
        related_files=[
            _FakeRelatedFile(
                file=related_file,
                relationship=_FakeFileRelationshipRow(
                    file_id=file.id, related_file_id=related_file.id
                ),
            )
        ],
        entity_links=[
            _FakeLinkedEntity(link=_FakeFileEntityLink(entity_id=entity.id), entity=entity)
        ],
        lifecycle=_FakeFileLifecycle(),
    )
    fake_file_service.get_detail.return_value = detail

    response = client.get(f"/v1/files/{file.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Report.pdf"
    assert body["web_view_link"] == file.web_view_link
    assert body["metadata"]["normalized_extension"] == "pdf"
    assert body["classification"]["document_type"] == "Documentation"
    assert body["extraction"]["status"] == "success"
    assert body["intelligence"]["document_type"] == "contract"
    assert body["intelligence"]["summary"] == "A services agreement between two parties."
    assert body["intelligence"]["entities"][0]["value"] == "Acme Corp"
    assert body["knowledge_attributes"][0]["attribute_type"] == "department"
    assert body["related_files"][0]["name"] == "Report_v1.pdf"
    assert body["related_files"][0]["relationship_type"] == "sequential_version"
    assert body["entity_links"][0]["entity_name"] == "Phoenix"
    assert body["entity_links"][0]["entity_type"] == "project"
    assert body["lifecycle"]["state"] == "active"


def test_get_file_detail_returns_not_found_for_another_organizations_file(
    as_member, fake_file_service
) -> None:
    fake_file_service.get_detail.side_effect = NotFoundError("File not found.")

    response = client.get(f"/v1/files/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_file_detail_handles_a_file_with_no_enrichment_yet(
    as_member, fake_file_service
) -> None:
    file = _FakeFile()
    detail = _FakeFileDetail(
        file=file,
        metadata=None,
        classification=None,
        extraction=None,
        intelligence=None,
        knowledge_attributes=[],
        related_files=[],
    )
    fake_file_service.get_detail.return_value = detail

    response = client.get(f"/v1/files/{file.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"] is None
    assert body["classification"] is None
    assert body["extraction"] is None
    assert body["intelligence"] is None
    assert body["knowledge_attributes"] == []
    assert body["related_files"] == []


def test_download_file_requires_authentication(fake_file_service) -> None:
    response = client.get(f"/v1/files/{uuid.uuid4()}/download")

    assert response.status_code == 401


def test_download_file_streams_the_bytes(as_member, fake_file_service) -> None:
    file = _FakeFile()
    fake_file_service.get_download_stream.return_value = (
        file,
        iter([b"pdf-bytes"]),
        "application/pdf",
        "Report.pdf",
    )

    response = client.get(f"/v1/files/{file.id}/download")

    assert response.status_code == 200
    assert response.content == b"pdf-bytes"
    assert response.headers["content-type"] == "application/pdf"
    assert "Report.pdf" in response.headers["content-disposition"]


def test_download_file_handles_a_non_ascii_name(as_member, fake_file_service) -> None:
    """Regression guard, same class of bug the archive download endpoint
    hit: Content-Disposition must never crash on a non-ASCII filename."""
    file = _FakeFile(name="Café Menu — Draft.pdf")
    fake_file_service.get_download_stream.return_value = (
        file,
        iter([b"pdf-bytes"]),
        "application/pdf",
        "Café Menu — Draft.pdf",
    )

    response = client.get(f"/v1/files/{file.id}/download")

    assert response.status_code == 200
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_download_file_returns_not_found_for_another_organizations_file(
    as_member, fake_file_service
) -> None:
    fake_file_service.get_download_stream.side_effect = NotFoundError("File not found.")

    response = client.get(f"/v1/files/{uuid.uuid4()}/download")

    assert response.status_code == 404


class _FakeFolder:
    def __init__(self, *, name: str = "Finance") -> None:
        self.id = uuid.uuid4()
        self.provider_file_id = f"drive-folder-{name}"
        self.name = name
        self.path = f"/{name}"


def test_list_trashed_files_requires_authentication(fake_file_service) -> None:
    response = client.get(f"/v1/connectors/{uuid.uuid4()}/trash")
    assert response.status_code == 401


def test_list_trashed_files_returns_a_paginated_envelope(as_member, fake_file_service) -> None:
    fake_file_service.list_trashed_for_connector.return_value = ([_FakeFile()], 1)

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/trash")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_list_trashed_files_returns_not_found_for_another_organizations_connector(
    as_member, fake_file_service
) -> None:
    fake_file_service.list_trashed_for_connector.side_effect = NotFoundError("Connector not found.")

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/trash")

    assert response.status_code == 404


def test_list_trashed_files_flags_which_are_already_archived(
    as_member, fake_file_service, fake_archive_service
) -> None:
    archived = _FakeFile(name="Archived.pdf")
    not_archived = _FakeFile(name="NotArchived.pdf")
    fake_file_service.list_trashed_for_connector.return_value = ([archived, not_archived], 2)
    fake_archive_service.list_archived_file_ids.return_value = {archived.id}

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/trash")

    assert response.status_code == 200
    items = {item["name"]: item["is_archived"] for item in response.json()["items"]}
    assert items["Archived.pdf"] is True
    assert items["NotArchived.pdf"] is False


def test_search_folders_requires_authentication(fake_file_service) -> None:
    response = client.get(f"/v1/connectors/{uuid.uuid4()}/folders")
    assert response.status_code == 401


def test_search_folders_returns_matching_folders(as_member, fake_file_service) -> None:
    folder = _FakeFolder()
    fake_file_service.search_folders.return_value = [folder]

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/folders?query=fin")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Finance"
    assert body[0]["provider_file_id"] == folder.provider_file_id


def test_search_folders_returns_not_found_for_another_organizations_connector(
    as_member, fake_file_service
) -> None:
    fake_file_service.search_folders.side_effect = NotFoundError("Connector not found.")

    response = client.get(f"/v1/connectors/{uuid.uuid4()}/folders")

    assert response.status_code == 404
