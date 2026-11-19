class VaultError(Exception):
    """Base class for every typed error raised by domain/application code.

    Per Engineering Handbook §26: domain and application layers raise these,
    never raw strings or provider-specific exceptions. Only the presentation
    layer (FastAPI exception handlers) translates one of these into an HTTP
    response shape.
    """

    http_status: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(VaultError):
    http_status = 404
    code = "not_found"


class ValidationError(VaultError):
    http_status = 422
    code = "validation_error"


class UnauthorizedError(VaultError):
    """Not authenticated — missing, invalid, or expired credentials."""

    http_status = 401
    code = "unauthorized"


class ReauthRequiredError(UnauthorizedError):
    """A connector's refresh token was rejected by the provider as expired or
    revoked (Google's `invalid_grant`) — a *permanent* failure distinct from
    a transient/misconfigured auth error: no retry can ever succeed, and the
    only fix is the user reconnecting. Subclasses `UnauthorizedError` so
    existing `except UnauthorizedError` call sites keep working unchanged;
    callers that need to react specifically (mark a connector
    REAUTH_REQUIRED rather than a generic ERROR) catch this narrower type."""

    code = "reauth_required"


class ForbiddenError(VaultError):
    """Authenticated, but the identified user's role doesn't permit this
    action — distinct from UnauthorizedError (Handbook §13.1: Authentication
    vs. Authorization are different layers)."""

    http_status = 403
    code = "forbidden"


class ConflictError(VaultError):
    http_status = 409
    code = "conflict"


class RateLimitExceededError(VaultError):
    http_status = 429
    code = "rate_limit_exceeded"


class DependencyUnavailableError(VaultError):
    """Raised when a required infrastructure dependency (database, queue,
    external API) cannot be reached — distinct from a domain-level error."""

    http_status = 503
    code = "dependency_unavailable"


class AIUnavailableError(DependencyUnavailableError):
    """The AI reasoning service (GLM via OpenRouter) could not produce a
    usable answer. Deliberately *not* an `UnauthorizedError` even when the
    provider rejects our API key: a 401 from the AI provider means the
    server's key is bad, not that the caller's session expired, and
    surfacing it as HTTP 401 would make the frontend log the user out.
    `reason` lets callers degrade precisely (retry, fall back to
    deterministic output, or fail fast)."""

    code = "ai_unavailable"

    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"
    QUOTA_EXCEEDED = "quota_exceeded"
    REJECTED_REQUEST = "rejected_request"
    _NON_RETRYABLE = frozenset(
        {NOT_CONFIGURED, AUTH_FAILED, INVALID_RESPONSE, QUOTA_EXCEEDED, REJECTED_REQUEST}
    )

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        retry_after_seconds: float | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, details={**(details or {}), "reason": reason})
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.reason not in self._NON_RETRYABLE
