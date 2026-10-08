from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import requests

from vault_shared.logging import get_logger, get_request_id
from vault_shared.storage.errors import (
    StorageConflictError,
    StorageError,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StorageRateLimitedError,
    StorageUnauthorizedError,
    StorageUnavailableError,
)
from vault_shared.storage.models import FOLDER_MIME_TYPE

logger = get_logger("vault_shared.connectors.google_drive")

PROVIDER_NAME = "google_workspace"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API_BASE = "https://www.googleapis.com/upload/drive/v3"
_REQUEST_TIMEOUT_SECONDS = 30
_UPLOAD_TIMEOUT_SECONDS = 300
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
# Thumbnails are served from Google's image CDN, not the Drive API host; the
# bearer token is only ever sent to hosts under this suffix.
_THUMBNAIL_HOST_SUFFIX = ".googleusercontent.com"
GOOGLE_FOLDER_MIME_TYPE = FOLDER_MIME_TYPE
# Drive enforces no real limit on a file's `name` — some files (notably ones
# with no explicit title, where Drive falls back to a content excerpt) can
# return names far longer than any reasonable filename, which would
# otherwise overflow `folders.name`/`files.name` (String(1024)) and abort
# the whole ingest batch. Truncated here, at the connector boundary, so
# nothing downstream needs to know Drive can misbehave this way.
_MAX_NAME_LENGTH = 1024
_FILE_FIELDS = (
    "id,name,mimeType,parents,size,createdTime,modifiedTime,viewedByMeTime,"
    "owners(emailAddress),shared,md5Checksum,headRevisionId,trashed,webViewLink"
)


@dataclass(frozen=True)
class DriveFile:
    """Provider-shaped metadata, already parsed — the scanner service maps
    this onto the provider-neutral `Folder`/`File` DB models. Never carries
    file contents (Phase 4 spec: "do not download full file contents")."""

    id: str
    name: str
    mime_type: str
    parents: list[str]
    size: int | None
    created_time: datetime | None
    modified_time: datetime | None
    viewed_by_me_time: datetime | None
    owner_email: str | None
    shared: bool
    checksum: str | None
    version_id: str | None
    is_folder: bool
    trashed: bool
    web_view_link: str | None = None


@dataclass(frozen=True)
class DriveFilesPage:
    files: list[DriveFile]
    next_page_token: str | None


@dataclass(frozen=True)
class SharedDrive:
    id: str
    name: str


@dataclass(frozen=True)
class DriveChangesPage:
    changed_files: list[DriveFile]
    removed_file_ids: list[str]
    next_page_token: str | None
    new_start_page_token: str | None


@dataclass(frozen=True)
class DrivePermission:
    type: str
    role: str
    email_address: str | None
    domain: str | None


class GoogleDriveClient:
    """The only module that calls Google Drive's Files/Drives/Changes API —
    mirrors the OAuth client's role for the token dance (Handbook §8.1's
    "leaf module" pattern). Always called with an access token already
    refreshed by `ConnectorTokenService`; this class has no knowledge of
    OAuth, encryption, or the database."""

    def list_shared_drives(self, *, access_token: str) -> list[SharedDrive]:
        drives: list[SharedDrive] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "pageSize": 100,
                "fields": "nextPageToken,drives(id,name)",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(
                f"{DRIVE_API_BASE}/drives", access_token=access_token, params=params
            )
            drives.extend(
                SharedDrive(id=d["id"], name=d["name"]) for d in payload.get("drives", [])
            )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return drives

    def list_files_page(
        self,
        *,
        access_token: str,
        drive_id: str | None,
        page_token: str | None,
        page_size: int = 1000,
    ) -> DriveFilesPage:
        params: dict[str, str | int] = {
            "pageSize": page_size,
            "fields": f"nextPageToken,files({_FILE_FIELDS})",
            "q": "trashed = false",
            "spaces": "drive",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "corpora": "drive" if drive_id else "user",
        }
        if drive_id:
            params["driveId"] = drive_id
        if page_token:
            params["pageToken"] = page_token

        payload = self._get(f"{DRIVE_API_BASE}/files", access_token=access_token, params=params)
        files = [self._to_drive_file(item) for item in payload.get("files", [])]
        return DriveFilesPage(files=files, next_page_token=payload.get("nextPageToken"))

    def get_start_page_token(self, *, access_token: str, drive_id: str | None) -> str:
        params: dict[str, str] = {}
        if drive_id:
            params["driveId"] = drive_id
        payload = self._get(
            f"{DRIVE_API_BASE}/changes/startPageToken", access_token=access_token, params=params
        )
        return str(payload["startPageToken"])

    def list_changes_page(
        self, *, access_token: str, page_token: str, drive_id: str | None
    ) -> DriveChangesPage:
        params: dict[str, str] = {
            "pageToken": page_token,
            "fields": (
                f"nextPageToken,newStartPageToken,changes(fileId,removed,file({_FILE_FIELDS}))"
            ),
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "spaces": "drive",
        }
        if drive_id:
            params["driveId"] = drive_id
        payload = self._get(f"{DRIVE_API_BASE}/changes", access_token=access_token, params=params)

        changed: list[DriveFile] = []
        removed: list[str] = []
        for change in payload.get("changes", []):
            if change.get("removed"):
                removed.append(change["fileId"])
            elif change.get("file"):
                changed.append(self._to_drive_file(change["file"]))

        return DriveChangesPage(
            changed_files=changed,
            removed_file_ids=removed,
            next_page_token=payload.get("nextPageToken"),
            new_start_page_token=payload.get("newStartPageToken"),
        )

    def download_file(self, *, access_token: str, file_id: str) -> bytes:
        """Downloads raw bytes for a binary Drive file — used only by the
        Knowledge Engine's content extraction (Phase 5), never by the
        Scanner. Still strictly read-only against Drive; the caller decides
        whether a file is worth downloading at all (e.g. a size check
        against the already-scanned `File.size_bytes`) before calling this,
        since this client has no concept of an extraction size policy."""
        return self._get_bytes(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"alt": "media"},
            content_access=True,
        )

    def export_file(self, *, access_token: str, file_id: str, export_mime_type: str) -> bytes:
        """Exports a native Google Workspace file (Docs/Sheets/Slides have no
        raw bytes of their own) to a requested MIME type — e.g. a Google Doc
        exported as `text/plain` for the extraction framework."""
        return self._get_bytes(
            f"{DRIVE_API_BASE}/files/{file_id}/export",
            access_token=access_token,
            params={"mimeType": export_mime_type},
            content_access=True,
        )

    def get_file(self, *, access_token: str, file_id: str) -> DriveFile:
        """A single-file live read — used by the Execution Engine (Phase 8)
        to re-check a file's current state (parent, name, trashed) right
        before mutating it, since the plan may have been created from a
        stale scan. Still read-only; the write methods below are the only
        ones that can mutate anything."""
        payload = self._get(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return self._to_drive_file(payload)

    def move_file(
        self, *, access_token: str, file_id: str, add_parent_id: str, remove_parent_id: str
    ) -> DriveFile:
        """The Execution Engine's `move_file`/`move_folder` action (Phase 8)
        — Drive represents "move" as adding the new parent and removing the
        old one, not a single "set parent" call. Both parent ids are
        required (not inferred) so the caller — which already captured the
        file's pre-move state for `RollbackRecord` — is the single source
        of truth for what "moving back" means, not a second live read."""
        payload = self._patch(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={
                "addParents": add_parent_id,
                "removeParents": remove_parent_id,
                "fields": _FILE_FIELDS,
                "supportsAllDrives": "true",
            },
            json_body={},
        )
        return self._to_drive_file(payload)

    def rename_file(self, *, access_token: str, file_id: str, new_name: str) -> DriveFile:
        """The Execution Engine's `rename` action. Rollback is the same
        call with the pre-rename name captured in `RollbackRecord`."""
        payload = self._patch(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            json_body={"name": new_name[:_MAX_NAME_LENGTH]},
        )
        return self._to_drive_file(payload)

    def set_trashed(self, *, access_token: str, file_id: str, trashed: bool) -> DriveFile:
        """The Execution Engine's `archive`/`remove_duplicate` actions —
        implemented as Drive's own Trash (`trashed: true`), deliberately
        *not* permanent deletion (`files.delete`, see `delete_file` below).
        Trashed items remain recoverable directly in Drive and via
        `trashed: false` here, which is exactly what makes both actions
        genuinely reversible rather than "irreversible unless the founder
        digs through Drive's own trash UI in time.\""""
        payload = self._patch(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            json_body={"trashed": trashed},
        )
        return self._to_drive_file(payload)

    def delete_file(self, *, access_token: str, file_id: str) -> None:
        """Real, unrecoverable deletion (`files.delete`) — the one Drive
        mutation every other Execution Engine action deliberately avoids
        (see `set_trashed` above). Only ever called by the
        `PERMANENT_DELETE` action, itself only reachable from a file
        that's already sitting in Drive's own Trash: Google's own storage
        accounting keeps counting a trashed file against quota until it's
        either emptied from Trash in Drive directly or deleted this way."""
        self._request(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"supportsAllDrives": "true"},
            method="DELETE",
        )

    def list_trashed_files(self, *, access_token: str) -> list[DriveFile]:
        """Everything this account owns that sits in Drive's Trash — exactly
        what `empty_trash` would permanently delete."""
        files: list[DriveFile] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "pageSize": 1000,
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "q": "trashed = true and 'me' in owners",
                "spaces": "drive",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(f"{DRIVE_API_BASE}/files", access_token=access_token, params=params)
            files.extend(self._to_drive_file(item) for item in payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return files

    def empty_drive_trash(self, *, access_token: str) -> None:
        """Permanently deletes every file this account owns in Drive's Trash
        (`files.emptyTrash`). Unrecoverable."""
        self._request(
            f"{DRIVE_API_BASE}/files/trash", access_token=access_token, params={}, method="DELETE"
        )

    def update_app_properties(
        self, *, access_token: str, file_id: str, properties: dict[str, str]
    ) -> DriveFile:
        """The Execution Engine's `update_metadata` action — Drive's
        `appProperties` are private key-value pairs visible only to the
        app that set them (not shown in Drive's own UI), the "where
        supported" metadata surface Phase 8's spec anticipates without
        needing write access to Drive's own description/properties field
        that other apps or the file owner might also rely on."""
        payload = self._patch(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            json_body={"appProperties": properties},
        )
        return self._to_drive_file(payload)

    def stream_file(self, *, access_token: str, file_id: str) -> Iterator[bytes]:
        """`download_file`, but as a stream: the response is read in bounded
        chunks and never held whole in memory. The request is issued (and any
        error raised) when this is called, not on first iteration; closing
        the returned iterator releases the connection, which is how a caller
        abandons an oversized download mid-transfer."""
        response = self._request(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"alt": "media", "supportsAllDrives": "true"},
            content_access=True,
            stream=True,
        )
        return _iter_response(response)

    def stream_export(
        self, *, access_token: str, file_id: str, export_mime_type: str
    ) -> Iterator[bytes]:
        response = self._request(
            f"{DRIVE_API_BASE}/files/{file_id}/export",
            access_token=access_token,
            params={"mimeType": export_mime_type},
            content_access=True,
            stream=True,
        )
        return _iter_response(response)

    def copy_file(
        self,
        *,
        access_token: str,
        file_id: str,
        new_name: str | None,
        parent_id: str | None,
    ) -> DriveFile:
        body: dict = {}
        if new_name is not None:
            body["name"] = new_name[:_MAX_NAME_LENGTH]
        if parent_id is not None:
            body["parents"] = [parent_id]
        response = self._request(
            f"{DRIVE_API_BASE}/files/{file_id}/copy",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            method="POST",
            json_body=body,
        )
        return self._to_drive_file(response.json())

    def create_folder(self, *, access_token: str, name: str, parent_id: str | None) -> DriveFile:
        body: dict = {"name": name[:_MAX_NAME_LENGTH], "mimeType": FOLDER_MIME_TYPE}
        if parent_id is not None:
            body["parents"] = [parent_id]
        response = self._request(
            f"{DRIVE_API_BASE}/files",
            access_token=access_token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            method="POST",
            json_body=body,
        )
        return self._to_drive_file(response.json())

    def list_permissions(self, *, access_token: str, file_id: str) -> list[DrivePermission]:
        permissions: list[DrivePermission] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "fields": "nextPageToken,permissions(type,role,emailAddress,domain)",
                "supportsAllDrives": "true",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(
                f"{DRIVE_API_BASE}/files/{file_id}/permissions",
                access_token=access_token,
                params=params,
            )
            permissions.extend(
                DrivePermission(
                    type=item.get("type", ""),
                    role=item.get("role", ""),
                    email_address=item.get("emailAddress"),
                    domain=item.get("domain"),
                )
                for item in payload.get("permissions", [])
            )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return permissions

    def get_thumbnail(self, *, access_token: str, file_id: str) -> bytes | None:
        """The item's thumbnail image, or None when Drive has none or points
        somewhere other than Google's image CDN (the bearer token is never
        sent to an unexpected host)."""
        payload = self._get(
            f"{DRIVE_API_BASE}/files/{file_id}",
            access_token=access_token,
            params={"fields": "thumbnailLink", "supportsAllDrives": "true"},
        )
        link = payload.get("thumbnailLink")
        if not link:
            return None
        parsed = urlparse(link)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith(_THUMBNAIL_HOST_SUFFIX):
            return None
        return self._get_bytes(link, access_token=access_token, params={})

    def upload_file(
        self,
        *,
        access_token: str,
        name: str,
        parent_id: str | None,
        mime_type: str,
        chunks: Iterator[bytes],
    ) -> DriveFile:
        """Drive's resumable upload protocol: one POST opens a session, then
        the content is PUT in 8 MiB pieces (Drive requires multiples of
        256 KiB) — never buffered whole, so archive size is bounded by the
        user's quota, not worker memory. The total size is only declared on
        the last piece, so `chunks` may be any stream."""
        metadata: dict = {"name": name[:_MAX_NAME_LENGTH], "mimeType": mime_type}
        if parent_id is not None:
            metadata["parents"] = [parent_id]
        started = self._request(
            f"{DRIVE_UPLOAD_API_BASE}/files",
            access_token=access_token,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            method="POST",
            json_body=metadata,
        )
        session_url = started.headers.get("Location", "")
        started.close()
        if (urlparse(session_url).hostname or "") != "www.googleapis.com":
            raise _drive_error(
                StorageUnavailableError,
                "Google Drive did not open an upload session.",
                provider_code="upload_not_started",
            )

        source = iter(chunks)
        pending = b""
        offset = 0
        exhausted = False
        while True:
            while len(pending) < _UPLOAD_CHUNK_BYTES and not exhausted:
                try:
                    pending += next(source)
                except StopIteration:
                    exhausted = True
            if exhausted:
                body = pending
                total = offset + len(body)
                content_range = (
                    f"bytes {offset}-{total - 1}/{total}" if body else f"bytes */{total}"
                )
            else:
                body = pending[:_UPLOAD_CHUNK_BYTES]
                content_range = f"bytes {offset}-{offset + len(body) - 1}/*"

            response = self._put_upload_chunk(
                session_url, access_token=access_token, body=body, content_range=content_range
            )
            if response.status_code in (200, 201):
                return self._to_drive_file(
                    self._get(
                        f"{DRIVE_API_BASE}/files/{response.json()['id']}",
                        access_token=access_token,
                        params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
                    )
                )
            # 308 Resume Incomplete: `Range` says how much Drive actually kept.
            received = response.headers.get("Range")
            next_offset = int(received.rsplit("-", 1)[-1]) + 1 if received else offset
            consumed = next_offset - offset
            pending = pending[consumed:]
            offset = next_offset
            if exhausted and not pending:
                raise _drive_error(
                    StorageUnavailableError,
                    "Google Drive did not finish the upload.",
                    provider_code="upload_incomplete",
                )

    def _put_upload_chunk(
        self, session_url: str, *, access_token: str, body: bytes, content_range: str
    ) -> requests.Response:
        try:
            response = requests.put(
                session_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Range": content_range,
                    "Content-Length": str(len(body)),
                },
                data=body,
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise _drive_error(
                StorageUnavailableError, "Google Drive timed out.", provider_code="timeout"
            ) from exc
        except requests.RequestException as exc:
            raise _drive_error(
                StorageUnavailableError,
                "Could not reach Google Drive.",
                provider_code="unreachable",
            ) from exc
        if response.status_code != 308:
            self._raise_for_status(response, url=session_url, content_access=False)
        return response

    def get_storage_quota(self, *, access_token: str) -> tuple[int | None, int | None, int | None]:
        """(used bytes, total bytes, bytes in Trash). Total is None for an
        unlimited plan — Drive omits `limit` then. Trashed files still count
        toward `usage` until the Trash is emptied."""
        payload = self._get(
            f"{DRIVE_API_BASE}/about",
            access_token=access_token,
            params={"fields": "storageQuota(limit,usage,usageInDriveTrash)"},
        )
        quota = payload.get("storageQuota") or {}

        def as_int(key: str) -> int | None:
            value = quota.get(key)
            return int(value) if value is not None else None

        return as_int("usage"), as_int("limit"), as_int("usageInDriveTrash")

    def get_account_email(self, *, access_token: str) -> str | None:
        """A cheap authenticated probe (Drive `about`) used for health."""
        payload = self._get(
            f"{DRIVE_API_BASE}/about",
            access_token=access_token,
            params={"fields": "user(emailAddress)"},
        )
        return (payload.get("user") or {}).get("emailAddress")

    def _get(self, url: str, *, access_token: str, params: dict) -> dict:
        response = self._request(url, access_token=access_token, params=params)
        return response.json()

    def _patch(self, url: str, *, access_token: str, params: dict, json_body: dict) -> dict:
        response = self._request(
            url, access_token=access_token, params=params, method="PATCH", json_body=json_body
        )
        return response.json()

    def _get_bytes(
        self, url: str, *, access_token: str, params: dict, content_access: bool = False
    ) -> bytes:
        response = self._request(
            url, access_token=access_token, params=params, content_access=content_access
        )
        return response.content

    def _request(
        self,
        url: str,
        *,
        access_token: str,
        params: dict,
        content_access: bool = False,
        method: str = "GET",
        json_body: dict | None = None,
        stream: bool = False,
    ) -> requests.Response:
        try:
            response = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                json=json_body,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                stream=stream,
            )
        except requests.Timeout as exc:
            raise _drive_error(
                StorageUnavailableError, "Google Drive timed out.", provider_code="timeout"
            ) from exc
        except requests.RequestException as exc:
            raise _drive_error(
                StorageUnavailableError,
                "Could not reach Google Drive.",
                provider_code="unreachable",
            ) from exc

        try:
            self._raise_for_status(response, url=url, content_access=content_access)
        except Exception:
            response.close()
            raise
        return response

    @staticmethod
    def _raise_for_status(response: requests.Response, *, url: str, content_access: bool) -> None:
        """Translates Drive's HTTP behavior into the normalized storage
        errors, so nothing above the Google adapter needs to know it. Only
        fixed strings and safe codes go into an error — never the URL (it can
        carry item ids and query parameters), headers or the response body."""
        status = response.status_code
        if 200 <= status < 300:  # `files.delete` answers 204 No Content
            return
        if status == 401:
            raise _drive_error(
                StorageUnauthorizedError,
                "Google Drive rejected the access token.",
                provider_code="unauthorized",
            )
        if status == 404:
            # A genuinely different signal from "Drive is unavailable" — the
            # Execution Engine needs to tell "this file no longer exists"
            # apart from a transient provider failure, since only one of
            # those should ever be retried.
            raise _drive_error(
                StorageNotFoundError, "Google Drive item not found.", provider_code="not_found"
            )
        if status == 429:
            raise _drive_error(
                StorageRateLimitedError,
                "Google Drive rate limit exceeded.",
                provider_code="rate_limited",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if status == 403 and content_access:
            # A file's *content* can be 403 even though its metadata listed
            # fine — the owner disabled download/copy/print for viewers on
            # this specific item (a real, permanent Drive permission model
            # quirk, not a connector-wide problem). Distinct from the
            # metadata branch below: this one will never succeed on retry and
            # must not be treated as "Drive is unavailable" for the whole job.
            raise _drive_error(
                StorageForbiddenError,
                "Google Drive denied content access to this file.",
                provider_code="content_forbidden",
            )
        if status == 403:
            # Drive represents *both* a genuine permission denial and a rate
            # limit as HTTP 403 (not just 429) — `error.errors[0].reason`
            # is the only way to tell them apart. Only a real permission
            # denial is permanent; a rate-limit or unrecognized reason is
            # retryable, the safe default (never silently drops a transient
            # failure).
            reason = _drive_error_reason(response)
            rate_limit_reasons = (
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "dailyLimitExceeded",
            )
            if reason is not None and reason not in rate_limit_reasons:
                raise _drive_error(
                    StorageForbiddenError,
                    "Google Drive denied access.",
                    provider_code=reason,
                )
            raise _drive_error(
                StorageRateLimitedError,
                "Google Drive is throttling requests.",
                provider_code=reason or "forbidden_unspecified",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if status in (409, 412):
            raise _drive_error(
                StorageConflictError,
                "Google Drive reported a conflicting state for this item.",
                provider_code=f"http_{status}",
            )
        if status in (400, 422):
            raise _drive_error(
                StorageInvalidRequestError,
                "Google Drive rejected the request as invalid.",
                provider_code=_drive_error_reason(response) or f"http_{status}",
            )
        logger.warning("google_drive_request_failed", extra={"status_code": status})
        raise _drive_error(
            StorageUnavailableError,
            f"Google Drive request failed ({status}).",
            provider_code=f"http_{status}",
        )

    @staticmethod
    def _to_drive_file(item: dict) -> DriveFile:
        return DriveFile(
            id=item["id"],
            name=item.get("name", "untitled")[:_MAX_NAME_LENGTH],
            mime_type=item.get("mimeType", ""),
            parents=item.get("parents", []),
            size=int(item["size"]) if "size" in item else None,
            created_time=_parse_time(item.get("createdTime")),
            modified_time=_parse_time(item.get("modifiedTime")),
            viewed_by_me_time=_parse_time(item.get("viewedByMeTime")),
            owner_email=(item.get("owners") or [{}])[0].get("emailAddress"),
            shared=bool(item.get("shared", False)),
            checksum=item.get("md5Checksum"),
            version_id=item.get("headRevisionId"),
            is_folder=item.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE,
            trashed=bool(item.get("trashed", False)),
            web_view_link=item.get("webViewLink"),
        )


class _ResponseStream:
    """A response body as a closeable iterator. A class rather than a
    generator so that `close()` releases the connection even if the caller
    never started iterating."""

    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self._chunks = response.iter_content(chunk_size=_STREAM_CHUNK_BYTES)

    def __iter__(self) -> "_ResponseStream":
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._chunks)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._response.close()


def _iter_response(response: requests.Response) -> Iterator[bytes]:
    return _ResponseStream(response)


def _drive_error(
    error_type: type[StorageError],
    message: str,
    *,
    provider_code: str,
    retry_after_seconds: float | None = None,
) -> StorageError:
    return error_type(
        message,
        provider=PROVIDER_NAME,
        provider_code=provider_code,
        retry_after_seconds=retry_after_seconds,
        request_id=get_request_id(),
    )


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not isinstance(raw, str):
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _drive_error_reason(response: requests.Response) -> str | None:
    """Best-effort read of Drive's `error.errors[0].reason` — safe to log
    (a fixed enum-like string, e.g. "insufficientFilePermissions", never
    request/token content), used only to tell a permanent permission denial
    apart from a 403-shaped rate limit."""
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except ValueError:
        return None
    return errors[0].get("reason") if errors else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
