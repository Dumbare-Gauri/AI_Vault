from celery import Celery
from celery.signals import setup_logging
from opentelemetry.instrumentation.celery import CeleryInstrumentor

from vault_shared import configure_logging, get_settings
from vault_shared.error_tracking import configure_sentry
from vault_shared.settings import enforce_production_settings
from vault_shared.tracing import configure_tracing

settings = get_settings()
enforce_production_settings(settings)

# Phase 10 (ADR-022) — both are no-ops without SENTRY_DSN/
# OTEL_EXPORTER_OTLP_ENDPOINT set, mirroring the backend's
# app/core/observability.py. CeleryInstrumentor wraps every task
# execution with a span regardless — a no-op span if tracing isn't
# actually configured.
configure_sentry(settings.service_name, app_kind="celery")
configure_tracing(settings.service_name)
CeleryInstrumentor().instrument()

celery_app = Celery(
    "vault_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "worker.tasks.health",
        "worker.tasks.scan",
        "worker.tasks.enrichment",
        "worker.tasks.embedding",
        "worker.tasks.recommendation",
        "worker.tasks.execution",
        "worker.tasks.workflow",
        "worker.tasks.scheduler",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Redelivers a task if the worker process dies mid-execution rather than
    # silently dropping it — required for Handbook §8.15's "no lost job state".
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    worker_max_tasks_per_child=1000,
    # Phase 9 — Automation Engine (ADR-021). A separate `celery beat`
    # process reads this schedule and enqueues `worker.scheduler.sweep`
    # every 60s; the sweep itself is idempotent/safe-to-skip (`Scheduler
    # Service`'s docstring), so this interval is a responsiveness/DB-load
    # tradeoff, not a correctness one — a `SCHEDULED` trigger fires within
    # ~60s of its cron time, not exactly on it.
    beat_schedule={
        "sweep-due-workflow-triggers": {
            "task": "worker.scheduler.sweep",
            "schedule": 60.0,
        },
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    # Takes over Celery's own logging setup so every worker log line is the
    # same structured JSON format the backend emits (Handbook §26).
    configure_logging(settings.service_name, settings.log_level)


# Side-effect import — registers the task_prerun/task_postrun metric
# handlers in worker/observability.py. Must happen after celery_app exists
# (the signal decorators don't need it directly, but this keeps import
# order obviously tied to "this app's Celery instance is now configured").
import worker.observability  # noqa: E402,F401
