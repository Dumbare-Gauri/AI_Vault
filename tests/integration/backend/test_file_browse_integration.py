"""Files shows each storage as the storage arranges it: folders inside
folders, each with only its own files, identically named files kept in their
own folders, and nothing from the other storage. Disposable index data only."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_storage_isolation_integration import _TwoWorkspaces, requires_infra

from app.application.file_service import FileService
from vault_shared import NotFoundError
from vault_shared.db.models import DriveType
from vault_shared.db.repositories import FileRepository, FolderRepository, StorageSourceRepository
from vault_shared.db.session import get_session_factory


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _folder(db: Session, source_id: uuid.UUID, parent, name: str):  # noqa: ANN001, ANN202
    path = f"{parent.path if parent else ''}/{name}"
    folder = FolderRepository(db).upsert(
        storage_source_id=source_id,
        provider_file_id=f"d-{uuid.uuid4().hex[:8]}",
        provider_parent_id=parent.provider_file_id if parent else None,
        parent_folder_id=parent.id if parent else None,
        name=name,
        path=path,
        owner_email=None,
        is_shared=False,
        provider_created_at=None,
        provider_modified_at=None,
        scanned_at=datetime.now(UTC),
    )
    db.commit()
    return folder


def _file(db: Session, source_id: uuid.UUID, parent, name: str):  # noqa: ANN001, ANN202
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source_id,
        provider_file_id=f"f-{uuid.uuid4().hex[:8]}",
        provider_parent_id=parent.provider_file_id if parent else None,
        parent_folder_id=parent.id if parent else None,
        name=name,
        path=f"{parent.path if parent else ''}/{name}",
        mime_type="text/plain",
        size_bytes=10,
        owner_email=None,
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    db.commit()
    return file


def _browse(db: Session, both: _TwoWorkspaces, where: str, **kwargs):  # noqa: ANN003, ANN202
    return FileService(db, storage=None).browse(  # type: ignore[arg-type]
        both.connector(where).id,
        organization_id=both.org.id,
        source_id=kwargs.get("source_id"),
        folder_id=kwargs.get("folder_id"),
        limit=kwargs.get("limit", 50),
        offset=kwargs.get("offset", 0),
    )


@requires_infra
def test_a_folder_shows_its_own_folders_and_files_only(db: Session) -> None:
    both = _TwoWorkspaces(db)
    source = both.drive_source.id
    site = _folder(db, source, None, "Site")
    docs = _folder(db, source, site, "docs")
    assets = _folder(db, source, site, "assets")
    in_docs = _file(db, source, docs, "notes.txt")
    in_assets = _file(db, source, assets, "notes.txt")
    _file(db, source, site, "index.html")

    top = _browse(db, both, "drive")
    inside_site = _browse(db, both, "drive", folder_id=site.id)
    inside_docs = _browse(db, both, "drive", folder_id=docs.id)

    assert [f.name for f in top.folders] == ["Site"]
    assert [f.name for f in inside_site.folders] == ["assets", "docs"]
    assert [f.name for f in inside_site.files] == ["index.html"]
    assert [f.id for f in inside_docs.files] == [in_docs.id]
    assert in_assets.id not in {f.id for f in inside_docs.files}
    assert [c.label for c in inside_docs.breadcrumbs] == ["Google Drive", "Site", "docs"]


@requires_infra
def test_each_authorized_folder_on_this_computer_is_a_top_level_folder(db: Session) -> None:
    both = _TwoWorkspaces(db)
    second = StorageSourceRepository(db).upsert(
        connector_id=both.local.id,
        provider_drive_id=f"disk-2-{uuid.uuid4().hex[:6]}",
        name="D:\\Media",
        drive_type=DriveType.LOCAL_FOLDER,
    )
    db.commit()
    _file(db, second.id, None, "clip.mp4")

    top = _browse(db, both, "local")
    media = _browse(db, both, "local", source_id=second.id)

    assert {s.name for s in top.sources} == {"C:\\Work", "D:\\Media"}
    assert [f.name for f in media.files] == ["clip.mp4"]
    assert [c.label for c in media.breadcrumbs][-1] == "D:\\Media"


@requires_infra
def test_another_storages_folder_cannot_be_browsed_from_this_one(db: Session) -> None:
    both = _TwoWorkspaces(db)
    drive_folder = _folder(db, both.drive_source.id, None, "Private")

    with pytest.raises(NotFoundError):
        _browse(db, both, "local", folder_id=drive_folder.id)


@requires_infra
def test_files_are_paged_and_trashed_files_are_not_shown(db: Session) -> None:
    both = _TwoWorkspaces(db)
    source = both.drive_source.id
    folder = _folder(db, source, None, "Many")
    files = [_file(db, source, folder, f"file-{n:02}.txt") for n in range(5)]
    FileRepository(db).mark_trashed(files[0], trashed=True)
    db.commit()

    first = _browse(db, both, "drive", folder_id=folder.id, limit=2, offset=0)
    second = _browse(db, both, "drive", folder_id=folder.id, limit=2, offset=2)

    assert first.total_files == 4
    assert [f.name for f in first.files] == ["file-01.txt", "file-02.txt"]
    assert [f.name for f in second.files] == ["file-03.txt", "file-04.txt"]
