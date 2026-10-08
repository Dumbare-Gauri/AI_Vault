from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.error_handlers import register_error_handlers
from app.core.logging_middleware import RequestContextMiddleware
from app.core.metrics_middleware import MetricsMiddleware
from app.core.observability import configure_observability, instrument_app
from app.core.security_headers_middleware import SecurityHeadersMiddleware
from app.infrastructure.cache.redis_client import get_redis
from app.presentation.api.health import health_router
from app.presentation.api.metrics import metrics_router
from app.presentation.api.v1.router import v1_router
from vault_shared import configure_logging, get_logger, get_settings
from vault_shared.ai_gateway import get_ai_gateway
from vault_shared.db.session import get_engine
from vault_shared.settings import enforce_production_settings

logger = get_logger("app.lifespan")


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Search performance fix: `get_ai_gateway()` is process-cached
    # (`@lru_cache`), but nothing warmed it before this — the embedding
    # provider lazily loads a ~128MB GloVe model on first use, which takes
    # 80-170s of blocking disk I/O/deserialization (measured). Left lazy,
    # whichever user's search or chat request happens to arrive first after
    # a process start pays that entire cost inline, indistinguishable from
    # a hang. Warming it here moves the cost to startup — before the
    # process accepts any traffic — where it's expected and off the
    # request path. Best-effort: a warmup failure must never block the
    # rest of the API (connectors/scans/files/etc. don't need this at all)
    # from starting; search/chat still work via their existing lazy-load
    # fallback if this doesn't run.
    try:
        get_ai_gateway().embed(["startup warmup"])
    except Exception:
        logger.warning("ai_gateway_warmup_failed_at_startup")

    yield
    # Graceful shutdown (Phase 10, ADR-022) — release the DB connection
    # pool and Redis client cleanly on SIGTERM instead of letting the
    # orchestrator kill sockets out from under an in-flight request.
    get_engine().dispose()
    try:
        get_redis().close()
    except Exception:
        logger.warning("redis_close_failed_during_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    enforce_production_settings(settings)
    configure_logging(settings.service_name, settings.log_level)
    configure_observability(settings.service_name)

    app = FastAPI(
        title="AI Project Vault API",
        version="1.0.0",
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
        expose_headers=["X-Request-Id"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(v1_router, prefix="/v1")

    instrument_app(app)

    return app


app = create_app()
