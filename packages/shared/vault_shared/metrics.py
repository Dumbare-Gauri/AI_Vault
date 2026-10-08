"""Prometheus metric definitions shared by apps/backend and apps/worker
(Phase 10, ADR-022) — one process-wide default registry, imported by both
apps so a counter incremented in the worker and one incremented in the
backend both show up under the same metric name with the same label
schema. Route/path labels must always be the *route template*
(`/v1/files/{file_id}`), never a resolved path containing a real ID —
using a real ID as a label value is an unbounded-cardinality mistake that
degrades Prometheus itself, not just this app.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST

HTTP_REQUESTS_TOTAL = Counter(
    "vault_http_requests_total",
    "Total HTTP requests handled by the backend",
    ["method", "path", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "vault_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
CELERY_TASKS_TOTAL = Counter(
    "vault_celery_tasks_total",
    "Total Celery tasks completed, by terminal status",
    ["task_name", "status"],
)
CELERY_TASK_DURATION_SECONDS = Histogram(
    "vault_celery_task_duration_seconds",
    "Celery task duration in seconds",
    ["task_name"],
)
QUEUE_DEPTH = Gauge(
    "vault_queue_depth",
    "Approximate Redis broker queue depth (Handbook §23's 'queue depth')",
    ["queue_name"],
)
AI_PROVIDER_LATENCY_SECONDS = Histogram(
    "vault_ai_provider_latency_seconds",
    "AI Gateway provider call latency in seconds",
    ["provider", "operation"],
)
AI_TOKENS_TOTAL = Counter(
    "vault_ai_tokens_total",
    "Tokens reported by the AI provider (cost proxy). Never carries content.",
    ["provider", "model"],
)
AI_FAILURES_TOTAL = Counter(
    "vault_ai_failures_total",
    "AI Gateway completion failures by normalized reason",
    ["provider", "reason"],
)
WORKFLOW_EXECUTIONS_TOTAL = Counter(
    "vault_workflow_executions_total",
    "Workflow executions by terminal status",
    ["status"],
)
ASSISTANT_TOOL_USED_TOTAL = Counter(
    "vault_assistant_tool_used_total",
    "AI Storage Assistant turns by which tool (if any) the deterministic "
    "intent classifier routed to — label 'none' means it fell through to "
    "semantic search, label 'rate_limited' means a tool match was skipped "
    "for exceeding the service-layer rate limit (ADR-024). A rising "
    "'none' share signals the classifier's rule table needs more coverage.",
    ["tool"],
)


def record_http_request(
    *, method: str, path: str, status_code: int, duration_seconds: float
) -> None:
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status_code=str(status_code)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration_seconds)


def record_celery_task(task_name: str, status: str, duration_seconds: float) -> None:
    CELERY_TASKS_TOTAL.labels(task_name=task_name, status=status).inc()
    CELERY_TASK_DURATION_SECONDS.labels(task_name=task_name).observe(duration_seconds)


def set_queue_depth(queue_name: str, depth: int) -> None:
    QUEUE_DEPTH.labels(queue_name=queue_name).set(depth)


def record_ai_provider_latency(*, provider: str, operation: str, duration_seconds: float) -> None:
    AI_PROVIDER_LATENCY_SECONDS.labels(provider=provider, operation=operation).observe(
        duration_seconds
    )


def record_ai_tokens(*, provider: str, model: str, tokens: int) -> None:
    AI_TOKENS_TOTAL.labels(provider=provider, model=model).inc(tokens)


def record_ai_failure(*, provider: str, reason: str) -> None:
    AI_FAILURES_TOTAL.labels(provider=provider, reason=reason).inc()


def record_workflow_execution(status: str) -> None:
    WORKFLOW_EXECUTIONS_TOTAL.labels(status=status).inc()


def record_assistant_tool_used(tool: str) -> None:
    ASSISTANT_TOOL_USED_TOTAL.labels(tool=tool).inc()


def generate_latest_metrics() -> bytes:
    return generate_latest()
