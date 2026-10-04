import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from vault_shared import get_logger, set_request_id

logger = get_logger("app.request")

# A caller-supplied correlation id is echoed into logs and forwarded to
# providers, so anything that isn't a short, plain identifier is replaced
# rather than trusted (log-forging / header-injection hygiene).
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def resolve_request_id(supplied: str | None) -> str:
    if supplied and _VALID_REQUEST_ID.match(supplied):
        return supplied
    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID (Observability, Handbook §23) and logs one
    structured line per request — method, path, status, duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = resolve_request_id(request.headers.get("x-request-id"))
        set_request_id(request_id)
        started_at = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request_handled",
            extra={
                "http_method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
