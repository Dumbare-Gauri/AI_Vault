import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.presentation.dependencies.auth import get_current_user
from app.presentation.dependencies.services import get_archive_service
from fastapi.testclient import TestClient
from vault_shared import NotFoundError

client = TestClient(app)


class _FakeArchiveJob:
    def __init__(self, *, name: str = "Archive - 2 files, ~5.2 MB", status: str = "completed") -> None:
        self.id = uuid.uuid4()
        self.organization_id = uuid.uuid4()
        self.execution_plan_id = uuid.uuid4()
        self.name = name
        self.status = status
        self.object_storage_key = f"archives/org/{self.id}.zip"
        self.original_size_bytes = 5_452_595
        self.compressed_size_bytes = 5_243_000
        self.file_count = 2
        self.manifest = [
            {
                "file_id": str(uuid.uuid4()),
                "name": "report.pdf",
                "path": "/report.pdf",
                "size_bytes": 1024,
                "mime_type": "application/pdf",
                "checksum_sha256": "abc123",
            }
        ]
        self.created_by_user_id = uuid.uuid4()
        self.created_at = datetime.now(UTC)
        self.completed_at = datetime.now(UTC)
        self.destination_path = "AI Vault Archive/2026/archive.zip"
        self.destination_web_view_link = None
        self.archive_sha256 = "def456"
        self.verified_at = datetime.now(UTC)
        self.remove_originals = False
        self.originals_removed_count = 0


@pytest.fixture
def fake_archive_service() -> MagicMock:
    service = MagicMock()
    app.dependency_overrides[get_archive_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_archive_service, None)


@pytest.fixture
def as_owner(owner_user):
    app.dependency_overrides[get_current_user] = lambda: owner_user
    yield owner_user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_member(member_user):
    app.dependency_overrides[get_current_user] = lambda: member_user
    yield member_user
    app.dependency_overrides.pop(get_current_user, None)


def test_list_archives_returns_items(as_member, fake_archive_service) -> None:
    fake_archive_service.list_for_organization.return_value = ([_FakeArchiveJob()], 1)

    response = client.get("/v1/archives")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_archive_returns_detail_with_manifest(as_member, fake_archive_service) -> None:
    fake_archive_service.get_owned.return_value = _FakeArchiveJob()

    response = client.get(f"/v1/archives/{uuid.uuid4()}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["manifest"]) == 1
    assert body["manifest"][0]["name"] == "report.pdf"


def test_get_archive_returns_not_found(as_member, fake_archive_service) -> None:
    fake_archive_service.get_owned.side_effect = NotFoundError("Archive not found.")

    response = client.get(f"/v1/archives/{uuid.uuid4()}")

    assert response.status_code == 404


def test_download_archive_streams_the_zip(as_member, fake_archive_service) -> None:
    archive_job = _FakeArchiveJob()
    fake_archive_service.get_download_stream.return_value = (archive_job, iter([b"zip-bytes"]))

    response = client.get(f"/v1/archives/{archive_job.id}/download")

    assert response.status_code == 200
    assert response.content == b"zip-bytes"
    assert response.headers["content-type"] == "application/zip"


def test_download_archive_handles_non_ascii_name(as_member, fake_archive_service) -> None:
    """Regression guard: an archive name with a non-ASCII character (e.g.
    the em-dash a previous version of the auto-generated name used) must
    not crash Content-Disposition header construction — Starlette encodes
    every header value as latin-1, which chokes on '—' verbatim."""
    archive_job = _FakeArchiveJob(name="Archive — 2 files, ~5.2 MB")
    fake_archive_service.get_download_stream.return_value = (archive_job, iter([b"zip-bytes"]))

    response = client.get(f"/v1/archives/{archive_job.id}/download")

    assert response.status_code == 200
    assert response.content == b"zip-bytes"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "filename*=UTF-8''" in disposition


def test_owner_can_delete_an_archive(as_owner, fake_archive_service) -> None:
    response = client.delete(f"/v1/archives/{uuid.uuid4()}")

    assert response.status_code == 204
    fake_archive_service.delete.assert_called_once()


def test_member_cannot_delete_an_archive(as_member, fake_archive_service) -> None:
    response = client.delete(f"/v1/archives/{uuid.uuid4()}")

    assert response.status_code == 403
    fake_archive_service.delete.assert_not_called()
