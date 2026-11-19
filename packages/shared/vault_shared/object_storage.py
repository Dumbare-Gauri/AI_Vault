from collections.abc import Iterator
from typing import IO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from vault_shared.errors import DependencyUnavailableError
from vault_shared.settings import get_settings

# Streamed in fixed-size chunks rather than loaded whole, so downloading a
# large archive doesn't hold the entire object in worker/backend memory.
_DOWNLOAD_CHUNK_BYTES = 5 * 1024 * 1024


class ObjectStorageClient:
    """The Archive MVP's only path to the object store — a thin S3-API
    wrapper that works against MinIO locally (via `endpoint_url`) and real
    S3 in production, sharing one implementation (Terraform already
    provisions `aws_s3_bucket.backups` for Postgres dumps; this is the same
    vendor, a different bucket). Unlike `GoogleDriveClient`, nothing else in
    this codebase is meant to call this directly except `ExecutionService`
    (archive creation/rollback) and the backend's archive read endpoints
    (list/detail/download/delete) — this client has no opinion about who
    calls it, that boundary is enforced by which modules import it."""

    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.object_storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.object_storage_endpoint_url or None,
            # Empty keys mean "use the server's own AWS role" (boto3's default
            # chain), never a blank credential.
            aws_access_key_id=settings.object_storage_access_key or None,
            aws_secret_access_key=settings.object_storage_secret_key or None,
            use_ssl=settings.object_storage_secure,
            config=Config(signature_version="s3v4"),
        )

    def ensure_bucket(self) -> None:
        """Idempotent — no Terraform/MinIO init script provisions this
        bucket ahead of time, so the first write path calls this."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def put_object(self, *, key: str, body: IO[bytes], content_type: str) -> None:
        try:
            self.ensure_bucket()
            self._client.upload_fileobj(
                body, self._bucket, key, ExtraArgs={"ContentType": content_type}
            )
        except ClientError as exc:
            raise DependencyUnavailableError(f"Object storage write failed: {exc}") from exc

    def get_object_stream(self, *, key: str) -> Iterator[bytes]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise DependencyUnavailableError(f"Object storage read failed: {exc}") from exc
        yield from response["Body"].iter_chunks(chunk_size=_DOWNLOAD_CHUNK_BYTES)

    def delete_object(self, *, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise DependencyUnavailableError(f"Object storage delete failed: {exc}") from exc
