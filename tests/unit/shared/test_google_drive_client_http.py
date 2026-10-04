"""The Google Drive HTTP layer's translation into normalized storage errors,
and the operations added for the storage contract. `requests` is mocked — no
network, no credentials."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from vault_shared.connectors.google_drive import GoogleDriveClient
from vault_shared.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from vault_shared.logging import set_request_id
from vault_shared.storage import (
    StorageConflictError,
    StorageError,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StorageRateLimitedError,
    StorageUnauthorizedError,
    StorageUnavailableError,
)

TOKEN = "ya29.TOP-SECRET-TOKEN"
FILE_JSON = {"id": "f1", "name": "a.txt", "mimeType": "text/plain", "parents": ["p1"]}


def _response(
    status: int = 200,
    body: dict | None = None,
    *,
    headers: dict | None = None,
    chunks: list[bytes] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body if body is not None else {}
    response.headers = headers or {}
    response.content = b"raw"
    response.iter_content.return_value = iter(chunks or [b"ab", b"cd"])
    return response


def _client() -> GoogleDriveClient:
    return GoogleDriveClient()


class TestStatusTranslation:
    @pytest.mark.parametrize(
        ("status", "expected", "legacy"),
        [
            (401, StorageUnauthorizedError, UnauthorizedError),
            (404, StorageNotFoundError, NotFoundError),
            (409, StorageConflictError, ConflictError),
            (412, StorageConflictError, ConflictError),
            (429, StorageRateLimitedError, DependencyUnavailableError),
            (400, StorageInvalidRequestError, ValidationError),
            (500, StorageUnavailableError, DependencyUnavailableError),
            (502, StorageUnavailableError, DependencyUnavailableError),
            (503, StorageUnavailableError, DependencyUnavailableError),
        ],
    )
    def test_each_status_maps_to_one_normalized_error(
        self, status: int, expected: type[StorageError], legacy: type[Exception]
    ) -> None:
        with patch("requests.request", return_value=_response(status)):
            with pytest.raises(expected) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert isinstance(caught.value, legacy)
        assert caught.value.provider == "google_workspace"

    def test_a_403_with_a_permission_reason_is_forbidden(self) -> None:
        body = {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}
        with patch("requests.request", return_value=_response(403, body)):
            with pytest.raises(StorageForbiddenError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.provider_code == "insufficientFilePermissions"
        assert caught.value.retryable is False

    @pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded", None])
    def test_a_403_that_is_really_a_rate_limit_is_retryable(self, reason: str | None) -> None:
        body = {"error": {"errors": [{"reason": reason}]}} if reason else {}
        with patch("requests.request", return_value=_response(403, body)):
            with pytest.raises(StorageRateLimitedError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.retryable is True

    def test_a_content_403_is_a_permanent_per_item_denial(self) -> None:
        with patch("requests.request", return_value=_response(403)):
            with pytest.raises(StorageForbiddenError) as caught:
                _client().download_file(access_token=TOKEN, file_id="f1")

        assert caught.value.provider_code == "content_forbidden"

    def test_a_429_carries_retry_after_from_the_header(self) -> None:
        with patch("requests.request", return_value=_response(429, headers={"Retry-After": "30"})):
            with pytest.raises(StorageRateLimitedError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.retry_after_seconds == 30.0

    @pytest.mark.parametrize("header", ["soon", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
    def test_an_unusable_retry_after_is_ignored(self, header: str) -> None:
        with patch(
            "requests.request", return_value=_response(429, headers={"Retry-After": header})
        ):
            with pytest.raises(StorageRateLimitedError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.retry_after_seconds is None

    def test_a_timeout_is_unavailable_with_a_timeout_code(self) -> None:
        with patch("requests.request", side_effect=requests.Timeout("slow")):
            with pytest.raises(StorageUnavailableError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.provider_code == "timeout"

    def test_a_connection_error_is_unavailable(self) -> None:
        with patch("requests.request", side_effect=requests.ConnectionError("no route")):
            with pytest.raises(StorageUnavailableError) as caught:
                _client().get_file(access_token=TOKEN, file_id="f1")

        assert caught.value.provider_code == "unreachable"

    def test_any_2xx_is_success(self) -> None:
        with patch("requests.request", return_value=_response(204)):
            _client().delete_file(access_token=TOKEN, file_id="f1")

    def test_the_ambient_request_id_is_attached_for_correlation(self) -> None:
        set_request_id("req-abc")
        try:
            with patch("requests.request", return_value=_response(500)):
                with pytest.raises(StorageUnavailableError) as caught:
                    _client().get_file(access_token=TOKEN, file_id="f1")
        finally:
            set_request_id(None)

        assert caught.value.request_id == "req-abc"

    @pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500])
    def test_errors_never_contain_the_token_the_url_or_the_item_id(self, status: int) -> None:
        with patch("requests.request", return_value=_response(status)):
            with pytest.raises(StorageError) as caught:
                _client().get_file(access_token=TOKEN, file_id="SENSITIVE-FILE-ID")

        rendered = f"{caught.value!s} {caught.value!r} {caught.value.details!r}"
        assert TOKEN not in rendered
        assert "SENSITIVE-FILE-ID" not in rendered
        assert "googleapis.com" not in rendered

    def test_a_failed_streaming_response_is_closed(self) -> None:
        response = _response(404)
        with patch("requests.request", return_value=response):
            with pytest.raises(StorageNotFoundError):
                _client().stream_file(access_token=TOKEN, file_id="f1")

        response.close.assert_called_once()


class TestStreaming:
    def test_the_request_is_issued_as_a_stream_and_chunks_are_yielded(self) -> None:
        response = _response(chunks=[b"ab", b"cd", b"ef"])
        with patch("requests.request", return_value=response) as request:
            stream = _client().stream_file(access_token=TOKEN, file_id="f1")

        assert request.call_args.kwargs["stream"] is True
        assert list(stream) == [b"ab", b"cd", b"ef"]

    def test_exhausting_the_stream_releases_the_connection(self) -> None:
        response = _response()
        with patch("requests.request", return_value=response):
            stream = _client().stream_file(access_token=TOKEN, file_id="f1")

        list(stream)

        response.close.assert_called()

    def test_closing_an_unstarted_stream_still_releases_the_connection(self) -> None:
        response = _response()
        with patch("requests.request", return_value=response):
            stream = _client().stream_file(access_token=TOKEN, file_id="f1")

        stream.close()  # type: ignore[attr-defined]

        response.close.assert_called_once()

    def test_a_stream_is_not_read_into_memory_by_the_client(self) -> None:
        response = _response()
        with patch("requests.request", return_value=response):
            _client().stream_file(access_token=TOKEN, file_id="f1")

        assert response.content is not None  # untouched: the attribute was never consumed
        response.iter_content.assert_called_once()

    def test_export_streams_with_the_requested_mime_type(self) -> None:
        with patch("requests.request", return_value=_response()) as request:
            stream = _client().stream_export(
                access_token=TOKEN, file_id="f1", export_mime_type="application/pdf"
            )

        assert request.call_args.kwargs["params"] == {"mimeType": "application/pdf"}
        assert list(stream) == [b"ab", b"cd"]


class TestAddedOperations:
    def test_copy_posts_the_new_name_and_parent(self) -> None:
        with patch("requests.request", return_value=_response(200, FILE_JSON)) as request:
            copied = _client().copy_file(
                access_token=TOKEN, file_id="src", new_name="Copy", parent_id="dest"
            )

        assert request.call_args.args[0] == "POST"
        assert request.call_args.args[1].endswith("/files/src/copy")
        assert request.call_args.kwargs["json"] == {"name": "Copy", "parents": ["dest"]}
        assert copied.id == "f1"

    def test_copy_with_no_overrides_posts_an_empty_body(self) -> None:
        with patch("requests.request", return_value=_response(200, FILE_JSON)) as request:
            _client().copy_file(access_token=TOKEN, file_id="src", new_name=None, parent_id=None)

        assert request.call_args.kwargs["json"] == {}

    def test_create_folder_posts_a_folder_mime_type_under_the_parent(self) -> None:
        folder = {**FILE_JSON, "mimeType": "application/vnd.google-apps.folder"}
        with patch("requests.request", return_value=_response(200, folder)) as request:
            created = _client().create_folder(access_token=TOKEN, name="Clients", parent_id="p1")

        body = request.call_args.kwargs["json"]
        assert body == {
            "name": "Clients",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["p1"],
        }
        assert created.is_folder is True

    def test_permissions_are_listed_across_pages(self) -> None:
        pages = [
            _response(
                200,
                {
                    "permissions": [{"type": "user", "role": "owner", "emailAddress": "a@x.com"}],
                    "nextPageToken": "t2",
                },
            ),
            _response(
                200, {"permissions": [{"type": "domain", "role": "reader", "domain": "x.com"}]}
            ),
        ]
        with patch("requests.request", side_effect=pages) as request:
            permissions = _client().list_permissions(access_token=TOKEN, file_id="f1")

        assert [(p.type, p.role, p.email_address, p.domain) for p in permissions] == [
            ("user", "owner", "a@x.com", None),
            ("domain", "reader", None, "x.com"),
        ]
        assert request.call_count == 2

    def test_a_thumbnail_is_fetched_from_googles_image_host(self) -> None:
        link = "https://lh3.googleusercontent.com/thumb/abc"
        responses = [_response(200, {"thumbnailLink": link}), _response(200)]
        with patch("requests.request", side_effect=responses) as request:
            thumbnail = _client().get_thumbnail(access_token=TOKEN, file_id="f1")

        assert thumbnail == b"raw"
        assert request.call_args_list[1].args[1] == link

    @pytest.mark.parametrize(
        "link",
        [
            "https://evil.example.com/steal",
            "http://lh3.googleusercontent.com/insecure",
            "https://googleusercontent.com.evil.example/x",
            "https://notgoogleusercontent.com/x",
        ],
    )
    def test_the_token_is_never_sent_to_an_unexpected_thumbnail_host(self, link: str) -> None:
        with patch("requests.request", return_value=_response(200, {"thumbnailLink": link})) as r:
            thumbnail = _client().get_thumbnail(access_token=TOKEN, file_id="f1")

        assert thumbnail is None
        assert r.call_count == 1  # the metadata read only; no second request with the token

    def test_no_thumbnail_link_is_none(self) -> None:
        with patch("requests.request", return_value=_response(200, {})):
            assert _client().get_thumbnail(access_token=TOKEN, file_id="f1") is None

    def test_account_email_comes_from_the_about_endpoint(self) -> None:
        with patch(
            "requests.request", return_value=_response(200, {"user": {"emailAddress": "o@x.com"}})
        ):
            assert _client().get_account_email(access_token=TOKEN) == "o@x.com"
