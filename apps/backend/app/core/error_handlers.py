from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from vault_shared import VaultError, get_logger

logger = get_logger("app.errors")


def _error_response(
    status_code: int, code: str, message: str, details: dict | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def register_error_handlers(app: FastAPI) -> None:
    """The presentation layer is the only layer that translates an error into
    an HTTP response shape (Engineering Handbook §26)."""

    @app.exception_handler(VaultError)
    async def handle_vault_error(_: Request, exc: VaultError) -> JSONResponse:
        return _error_response(exc.http_status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # `ctx` can hold the raw exception a validator raised, which is not
        # JSON-serializable — without this, any custom validator turned a
        # 422 into a 500.
        errors = [{k: v for k, v in error.items() if k != "ctx"} for error in exc.errors()]
        custom = [
            e["msg"].removeprefix("Value error, ") for e in errors if e.get("type") == "value_error"
        ]
        return _error_response(
            422,
            "validation_error",
            custom[0] if custom else "Request payload failed validation.",
            {"errors": jsonable_encoder(errors)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception")
        return _error_response(500, "internal_error", "An unexpected error occurred.")
