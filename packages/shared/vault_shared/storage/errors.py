"""The normalized storage error model.

Every storage adapter translates its provider's failures (HTTP status codes,
SDK exceptions, OS errors) into these types, so the application layer never
needs to understand any provider's error behavior.

Each type also subclasses the pre-existing application error that means the
same thing (`NotFoundError`, `ForbiddenError`, ...). That is deliberate:
every `except` clause written before storage was abstracted keeps working,
and the HTTP status and JSON `code` of an API error are unchanged. The two
exceptions are provider-auth failures — see `StorageUnauthorizedError`.

Only safe metadata is kept on an error: the provider name, a provider error
*code* (a fixed enum-like string, never a message body), a retry-after hint
and a correlation id. Tokens, request headers and URLs with credentials are
never stored.
"""

from typing import Self

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


class StorageError(VaultError):
    """Base of every normalized storage failure."""

    http_status = 502
    code = "storage_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        safe_details = dict(details or {})
        for key, value in (
            ("provider", provider),
            ("provider_code", provider_code),
            ("retry_after_seconds", retry_after_seconds),
            ("request_id", request_id),
        ):
            if value is not None:
                safe_details[key] = value
        super().__init__(message, details=safe_details)
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        self.provider_code = provider_code
        self.request_id = request_id

    def with_provider(self, provider: str) -> Self:
        """Fills in the provider on an error raised below the adapter (for
        example by a lower-level client that does not know its own
        adapter's name)."""
        if self.provider is None:
            self.provider = provider
            self.details["provider"] = provider
        return self


class StorageAuthenticationRequiredError(StorageError, ReauthRequiredError):
    """The stored grant is permanently unusable (revoked or expired refresh
    token, credentials missing): only the user reconnecting the storage can
    fix it. Not retryable. `http_status` is 502 rather than the 401 a plain
    `ReauthRequiredError` carries, because the *user's* session is fine — a
    401 makes the web client refresh and then discard the user's login."""

    http_status = 502
    code = ReauthRequiredError.code


class StorageUnauthorizedError(StorageError, UnauthorizedError):
    """The provider rejected the credential it was given (for example an
    access token revoked or expired mid-request). Same status reasoning as
    `StorageAuthenticationRequiredError`."""

    http_status = 502
    code = "storage_unauthorized"


class StorageForbiddenError(StorageError, ForbiddenError):
    """The credential is valid but may not do this to this item — a
    per-item, permanent condition, distinct from provider-wide failure."""

    http_status = ForbiddenError.http_status
    code = ForbiddenError.code


class StorageNotFoundError(StorageError, NotFoundError):
    http_status = NotFoundError.http_status
    code = NotFoundError.code


class StorageConflictError(StorageError, ConflictError):
    """The item is not in a state that allows the operation (name already
    taken, edited concurrently, wrong parent, precondition failed)."""

    http_status = ConflictError.http_status
    code = ConflictError.code


class StorageStaleRevisionError(StorageConflictError):
    """The item changed after the caller captured the revision it was
    acting on, so the operation was refused rather than applied blindly."""

    code = "stale_revision"


class StorageRateLimitedError(StorageError, DependencyUnavailableError):
    """The provider asked us to slow down. Retryable; `retry_after_seconds`
    carries the provider's hint when it gave one."""

    http_status = DependencyUnavailableError.http_status
    code = DependencyUnavailableError.code
    retryable = True


class StorageUnavailableError(StorageError, DependencyUnavailableError):
    """The provider could not be reached or failed on its side (timeout,
    connection error, 5xx). Retryable."""

    http_status = DependencyUnavailableError.http_status
    code = DependencyUnavailableError.code
    retryable = True


class StorageUnsupportedError(StorageError):
    """The adapter's provider does not offer this operation — see
    `StorageCapabilities`. Never retryable."""

    http_status = 501
    code = "storage_unsupported"


class StorageInvalidRequestError(StorageError, ValidationError):
    """The request itself is malformed for this provider (bad destination,
    invalid name, unsupported argument). Retrying cannot help."""

    http_status = ValidationError.http_status
    code = ValidationError.code


class StorageContentTooLargeError(StorageInvalidRequestError):
    """A bounded read hit its byte limit."""

    http_status = 413
    code = "content_too_large"
