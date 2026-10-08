from dataclasses import fields
from datetime import UTC, datetime

import pytest

from vault_shared.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    NotFoundError,
    ReauthRequiredError,
    UnauthorizedError,
    ValidationError,
    VaultError,
)
from vault_shared.storage import (
    Capability,
    ProviderFileId,
    Revision,
    StorageAuthenticationRequiredError,
    StorageCapabilities,
    StorageConflictError,
    StorageContentTooLargeError,
    StorageError,
    StorageFile,
    StorageForbiddenError,
    StorageInvalidRequestError,
    StorageNotFoundError,
    StorageRateLimitedError,
    StorageStaleRevisionError,
    StorageUnauthorizedError,
    StorageUnavailableError,
    StorageUnsupportedError,
    read_bounded,
)


class TestRevision:
    def test_different_tokens_are_a_difference(self) -> None:
        assert Revision(token="a").differs_from(Revision(token="b"))

    def test_equal_tokens_are_not(self) -> None:
        assert not Revision(token="a").differs_from(Revision(token="a"))

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (Revision(token=None), Revision(token="b")),
            (Revision(token="a"), Revision(token=None)),
            (Revision(token=None), Revision(token=None)),
        ],
    )
    def test_a_missing_token_never_claims_a_difference(
        self, left: Revision, right: Revision
    ) -> None:
        assert not left.differs_from(right)

    def test_a_modified_time_alone_is_never_a_difference(self) -> None:
        early = Revision(token="a", modified_at=datetime(2026, 1, 1, tzinfo=UTC))
        late = Revision(token="a", modified_at=datetime(2026, 6, 1, tzinfo=UTC))

        assert not early.differs_from(late)

    def test_it_round_trips_through_json_shaped_data(self) -> None:
        revision = Revision(token="rev-9", modified_at=datetime(2026, 3, 4, 5, 6, tzinfo=UTC))

        assert Revision.from_dict(revision.to_dict()) == revision

    @pytest.mark.parametrize(
        "garbage", [None, "x", 5, [], {"token": 5}, {"modified_at": "nonsense"}]
    )
    def test_garbage_never_raises(self, garbage: object) -> None:
        result = Revision.from_dict(garbage)

        assert result is None or (result.token is None and result.modified_at is None)


class TestStorageFile:
    def _file(self, name: str) -> StorageFile:
        return StorageFile(
            provider="p",
            provider_file_id=ProviderFileId("id"),
            name=name,
            mime_type="text/plain",
            is_folder=False,
        )

    @pytest.mark.parametrize(
        ("name", "extension"),
        [("a.PDF", "pdf"), ("archive.tar.gz", "gz"), ("noext", None), (".env", None), ("a.", None)],
    )
    def test_extension_is_derived_from_the_name(self, name: str, extension: str | None) -> None:
        assert self._file(name).extension == extension

    def test_it_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            self._file("a").name = "b"  # type: ignore[misc]


class TestCapabilities:
    def test_every_capability_name_is_a_field_and_vice_versa(self) -> None:
        assert {c.value for c in Capability} == {f.name for f in fields(StorageCapabilities)}

    def test_the_contract_names_every_required_capability(self) -> None:
        required = {
            "supports_trash",
            "supports_restore",
            "supports_permanent_delete",
            "supports_copy",
            "supports_create_folder",
            "supports_upload",
            "supports_download",
            "supports_change_feed",
            "supports_thumbnails",
            "supports_permissions",
            "supports_native_export",
            "supports_streaming",
            "supports_hash",
        }

        assert required <= {c.value for c in Capability}

    def test_nothing_is_supported_unless_declared(self) -> None:
        assert StorageCapabilities().supported() == frozenset()

    def test_supports_reflects_the_flags(self) -> None:
        capabilities = StorageCapabilities(supports_trash=True)

        assert capabilities.supports(Capability.TRASH)
        assert not capabilities.supports(Capability.RESTORE)
        assert capabilities.supported() == {Capability.TRASH}

    def test_require_raises_a_normalized_error_naming_the_operation(self) -> None:
        with pytest.raises(StorageUnsupportedError, match="copy") as caught:
            StorageCapabilities().require(Capability.COPY, provider="dropbox")

        assert caught.value.provider == "dropbox"

    def test_require_passes_for_a_supported_capability(self) -> None:
        StorageCapabilities(supports_copy=True).require(Capability.COPY, provider="p")


class TestErrors:
    @pytest.mark.parametrize(
        ("error_type", "legacy", "status", "code"),
        [
            (StorageNotFoundError, NotFoundError, 404, "not_found"),
            (StorageForbiddenError, ForbiddenError, 403, "forbidden"),
            (StorageConflictError, ConflictError, 409, "conflict"),
            (StorageRateLimitedError, DependencyUnavailableError, 503, "dependency_unavailable"),
            (StorageUnavailableError, DependencyUnavailableError, 503, "dependency_unavailable"),
            (StorageInvalidRequestError, ValidationError, 422, "validation_error"),
        ],
    )
    def test_existing_api_status_and_code_are_unchanged(
        self, error_type: type[StorageError], legacy: type[VaultError], status: int, code: str
    ) -> None:
        error = error_type("m", provider="p")

        assert isinstance(error, legacy)
        assert error.http_status == status
        assert error.code == code

    def test_provider_authentication_failures_are_never_http_401(self) -> None:
        for error in (
            StorageUnauthorizedError("m", provider="p"),
            StorageAuthenticationRequiredError("m", provider="p"),
        ):
            assert error.http_status != 401
        assert isinstance(StorageUnauthorizedError("m"), UnauthorizedError)
        assert isinstance(StorageAuthenticationRequiredError("m"), ReauthRequiredError)

    def test_reauth_keeps_its_existing_api_code(self) -> None:
        assert StorageAuthenticationRequiredError("m").code == "reauth_required"

    def test_new_error_kinds_have_their_own_codes(self) -> None:
        assert StorageUnsupportedError("m").code == "storage_unsupported"
        assert StorageUnsupportedError("m").http_status == 501
        assert StorageStaleRevisionError("m").code == "stale_revision"
        assert isinstance(StorageStaleRevisionError("m"), ConflictError)
        assert StorageContentTooLargeError("m").http_status == 413

    def test_only_transient_failures_are_retryable(self) -> None:
        retryable = {
            t
            for t in (
                StorageRateLimitedError,
                StorageUnavailableError,
                StorageNotFoundError,
                StorageForbiddenError,
                StorageConflictError,
                StorageUnauthorizedError,
                StorageAuthenticationRequiredError,
                StorageUnsupportedError,
                StorageInvalidRequestError,
            )
            if t("m").retryable
        }

        assert retryable == {StorageRateLimitedError, StorageUnavailableError}

    def test_safe_metadata_is_preserved_in_the_details(self) -> None:
        error = StorageRateLimitedError(
            "slow",
            provider="google_workspace",
            provider_code="rateLimitExceeded",
            retry_after_seconds=12.0,
            request_id="req-1",
        )

        assert error.details == {
            "provider": "google_workspace",
            "provider_code": "rateLimitExceeded",
            "retry_after_seconds": 12.0,
            "request_id": "req-1",
        }
        assert error.retry_after_seconds == 12.0

    def test_absent_metadata_is_left_out_of_the_details(self) -> None:
        assert StorageNotFoundError("m").details == {}

    def test_with_provider_only_fills_a_missing_provider(self) -> None:
        unset = StorageNotFoundError("m").with_provider("google_workspace")
        already = StorageNotFoundError("m", provider="dropbox").with_provider("google_workspace")

        assert unset.provider == "google_workspace"
        assert unset.details["provider"] == "google_workspace"
        assert already.provider == "dropbox"


class TestReadBounded:
    class _Stream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._iterator = iter(chunks)
            self.closed = False

        def __iter__(self) -> "TestReadBounded._Stream":
            return self

        def __next__(self) -> bytes:
            return next(self._iterator)

        def close(self) -> None:
            self.closed = True

    def test_returns_everything_within_the_limit_and_closes(self) -> None:
        stream = self._Stream([b"ab", b"cd"])

        assert read_bounded(stream, max_bytes=4) == b"abcd"
        assert stream.closed

    def test_stops_reading_as_soon_as_the_limit_is_exceeded(self) -> None:
        consumed: list[bytes] = []

        class Counting(self._Stream):
            def __next__(self) -> bytes:
                chunk = super().__next__()
                consumed.append(chunk)
                return chunk

        stream = Counting([b"aaa", b"bbb", b"ccc", b"ddd"])

        with pytest.raises(StorageContentTooLargeError):
            read_bounded(stream, max_bytes=5)

        assert len(consumed) == 2
        assert stream.closed

    def test_a_plain_iterator_without_close_is_fine(self) -> None:
        assert read_bounded(iter([b"x"]), max_bytes=1) == b"x"

    def test_an_empty_stream_is_empty_bytes(self) -> None:
        assert read_bounded(iter([]), max_bytes=1) == b""
