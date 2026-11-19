"""The zip a Google Drive archive is built in: files stream through it chunk
by chunk with ZIP64, so a file over 2 GB goes in whole, and the result is
read back before anything is uploaded."""

import io
import tempfile
import zipfile
from collections.abc import Iterator

import pytest

from vault_shared import ValidationError, get_settings
from worker.execution import execution_service
from worker.execution.execution_service import (
    _require_free_space,
    _verify_zip,
    _write_zip_entry,
)

_CHUNK = b"\0" * (8 * 1024 * 1024)


def _zeros(total: int) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        piece = _CHUNK[: min(len(_CHUNK), total - sent)]
        sent += len(piece)
        yield piece


def test_a_file_over_two_gigabytes_streams_into_a_readable_archive() -> None:
    size = 2 * 1024**3 + 4096
    with tempfile.TemporaryFile() as spool:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            written, _ = _write_zip_entry(archive, "disk.img", _zeros(size), store_only=False)

        _verify_zip(spool, expected_entries=1)
        spool.seek(0)
        with zipfile.ZipFile(spool) as archive:
            assert archive.getinfo("disk.img").file_size == size
    assert written == size


def test_a_damaged_archive_fails_verification() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "hello " * 1000)
    damaged = bytearray(buffer.getvalue())
    damaged[40:60] = b"\xff" * 20

    with pytest.raises(ValidationError):
        _verify_zip(io.BytesIO(bytes(damaged)), expected_entries=1)


def test_an_archive_missing_entries_fails_verification() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "a")

    with pytest.raises(ValidationError):
        _verify_zip(buffer, expected_entries=2)


def test_not_enough_temporary_space_is_refused_with_the_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = execution_service.shutil.disk_usage(tempfile.gettempdir())
    monkeypatch.setattr(
        execution_service.shutil, "disk_usage", lambda _path: usage._replace(free=10 * 1024**2)
    )
    monkeypatch.setattr(get_settings(), "archive_free_space_reserve_bytes", 0)

    with pytest.raises(ValidationError, match="needs about"):
        _require_free_space(50 * 1024**2, what="archive 'movie.mp4'")
