from unittest.mock import MagicMock, patch

import pytest
from vault_shared import (
    DependencyUnavailableError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from vault_shared.connectors.google_drive import GoogleDriveClient


def _response(status_code: int, json_body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    return response


def _client() -> GoogleDriveClient:
    return GoogleDriveClient()


class TestListFilesPageErrorClassification:
    """Step 9's retry-policy classification, exercised at the one place
    Drive's error shapes actually reach the scanner: `list_files_page`."""

    def test_401_is_unauthorized(self) -> None:
        with (
            patch("requests.request", return_value=_response(401, {})),
            pytest.raises(UnauthorizedError),
        ):
            _client().list_files_page(access_token="expired", drive_id=None, page_token=None)

    def test_404_is_not_found(self) -> None:
        with (
            patch("requests.request", return_value=_response(404, {})),
            pytest.raises(NotFoundError),
        ):
            _client().list_files_page(access_token="token", drive_id=None, page_token=None)

    def test_429_is_dependency_unavailable_and_retryable(self) -> None:
        with (
            patch("requests.request", return_value=_response(429, {})),
            pytest.raises(DependencyUnavailableError),
        ):
            _client().list_files_page(access_token="token", drive_id=None, page_token=None)

    def test_403_with_a_genuine_permission_reason_is_forbidden_and_permanent(self) -> None:
        body = {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}
        with (
            patch("requests.request", return_value=_response(403, body)),
            pytest.raises(ForbiddenError),
        ):
            _client().list_files_page(access_token="token", drive_id=None, page_token=None)

    def test_403_with_a_rate_limit_reason_is_retryable_not_forbidden(self) -> None:
        """Drive represents some rate limits as HTTP 403, not just 429 — this
        must NOT be misclassified as a permanent permission denial, or a
        legitimately transient failure would stop retrying and fail the
        whole scan outright."""
        body = {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
        with (
            patch("requests.request", return_value=_response(403, body)),
            pytest.raises(DependencyUnavailableError) as exc_info,
        ):
            _client().list_files_page(access_token="token", drive_id=None, page_token=None)
        assert not isinstance(exc_info.value, ForbiddenError)

    def test_403_with_no_parseable_body_falls_back_to_retryable(self) -> None:
        """The safe default when the reason can't be determined at all —
        never silently drop a possibly-transient failure."""
        response = MagicMock()
        response.status_code = 403
        response.json.side_effect = ValueError("not json")
        with (
            patch("requests.request", return_value=response),
            pytest.raises(DependencyUnavailableError) as exc_info,
        ):
            _client().list_files_page(access_token="token", drive_id=None, page_token=None)
        assert not isinstance(exc_info.value, ForbiddenError)


class TestDownloadFileContentAccess:
    def test_403_on_content_access_is_always_forbidden_regardless_of_reason(self) -> None:
        with (
            patch("requests.request", return_value=_response(403, {})),
            pytest.raises(ForbiddenError),
        ):
            _client().download_file(access_token="token", file_id="file-1")


class TestSuccessfulStatusCodes:
    def test_delete_file_treats_204_no_content_as_success(self) -> None:
        with patch("requests.request", return_value=_response(204, {})) as request:
            _client().delete_file(access_token="token", file_id="file-1")

        assert request.call_args.args[0] == "DELETE"

    def test_a_5xx_is_still_a_retryable_failure(self) -> None:
        with (
            patch("requests.request", return_value=_response(500, {})),
            pytest.raises(DependencyUnavailableError),
        ):
            _client().delete_file(access_token="token", file_id="file-1")
