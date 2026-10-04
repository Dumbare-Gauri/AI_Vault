from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-sourced configuration shared by apps/backend and apps/worker.

    Every field documented here must have a matching entry in the repo-root
    `.env.example` (Engineering Handbook §13 — secrets never live in source,
    only placeholders documenting what's required).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "vault-service"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://vault:vault@localhost:5432/vault"
    redis_url: str = "redis://localhost:6379/0"

    # >=32 bytes so PyJWT doesn't warn about weak HMAC key length even with
    # the unset-in-env default — still just a placeholder, never use in prod.
    jwt_secret: str = "changeme-in-env-use-a-real-32-byte-secret"
    # Google Identity Services (client-side sign-in, Phase 2) verifies ID
    # tokens against this as the expected audience. The same client_id/secret
    # pair is also used for Phase 3's Workspace Connector — a separate,
    # server-side OAuth interaction (Drive access, not user identity) that
    # needs its own redirect URI/scopes/state handling, see ADR-014.
    google_client_id: str = ""
    google_client_secret: str = ""

    frontend_url: str = "http://localhost:5173"
    cors_allow_origins: str = "http://localhost:5173"

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    refresh_cookie_name: str = "vault_refresh_token"
    # False for plain-http local dev; must be true anywhere served over HTTPS.
    cookie_secure: bool = False

    # Phase 3 — Google Workspace Connector (ADR-014). The redirect URI is a
    # *frontend* route: Google redirects the browser there, and that page
    # POSTs the code/state to the backend — the backend never receives a
    # browser-navigated redirect directly.
    google_workspace_redirect_uri: str = "http://localhost:5173/connectors/google/callback"
    # `drive.readonly` (Phases 3-7) is no longer sufficient once the
    # Execution Engine (Phase 8) can move/rename/trash a file — `drive.file`
    # was considered but only covers files the app itself created or the
    # user explicitly opened with it, not arbitrary pre-existing files a
    # recommendation might target, so this is the full `drive` scope
    # instead (Handbook §13's least-privilege rule: the narrowest scope
    # that satisfies the *current* phase's features — this is that scope
    # now that a write-capable phase exists, not a scope grabbed early).
    # A connector authorized before this phase only granted
    # `drive.readonly` (see `ConnectorCredentials.granted_scopes`) and must
    # be reconnected to pick up write access — see ADR-020.
    google_workspace_scopes: str = "openid email profile https://www.googleapis.com/auth/drive"
    oauth_state_ttl_seconds: int = 600
    # Fernet key (32 url-safe base64-encoded bytes) for encrypting OAuth
    # tokens at rest (Handbook §13 — "encrypt sensitive tokens before
    # persistence"). No safe default — generate with
    # `Fernet.generate_key()` and never reuse the .env.example placeholder
    # outside local development.
    connector_encryption_key: str = ""

    # Phase 8 — Execution Engine (ADR-020). `execution_max_retries`/
    # `execution_timeout_seconds` are read by the Celery task's retry
    # policy (worker.execution.run), the same shape as the scanner/
    # enrichment retry config; `approval_expiry_hours` is how long an
    # `ApprovalRequest` stays actionable before `ApprovalRequestRepository.
    # expire_if_overdue` marks it `EXPIRED`; `execution_rollback_enabled`
    # is a kill switch a founder can flip without a deploy if rollback
    # itself ever needs to be disabled.
    execution_max_retries: int = 3
    execution_timeout_seconds: int = 300
    approval_expiry_hours: int = 72
    execution_rollback_enabled: bool = True

    # Phase 9 — Automation Engine (ADR-021). `workflow_max_retries`/
    # `workflow_timeout_seconds` bound `worker.workflow.run`'s own retry/
    # wall-clock behavior, same shape as Phase 8's execution settings;
    # `scheduler_timezone` is the IANA zone cron expressions on `SCHEDULED`
    # triggers are interpreted in (e.g. "0 2 * * *" means 2am *here*, not
    # always UTC) — used by `croniter` in `WorkflowTriggerService`/
    # `SchedulerService`, independent of Celery's own broker timezone
    # (always UTC, unrelated); `notification_email_enabled` toggles the
    # email channel on/off without touching which provider backs it (the
    # founder's binding choice this phase: `StubEmailProvider`, never a
    # real send, regardless of this flag); `default_approval_timeout_hours`
    # is what a workflow's `APPROVAL` node uses for its `ApprovalRequest.
    # expires_at` when the node doesn't override it — same value as
    # `approval_expiry_hours` by default, kept as a separate setting since
    # the phase spec names both env vars independently.
    workflow_max_retries: int = 3
    workflow_timeout_seconds: int = 300
    scheduler_timezone: str = "UTC"
    notification_email_enabled: bool = False
    default_approval_timeout_hours: int = 72

    # Phase 10 — Production Hardening (ADR-022). `/v1/dashboard` is the
    # spec's own named example of an endpoint worth caching ("Dashboard
    # aggregation") — it's read by every page load of the Founder Command
    # Center but only actually changes once per scan/enrichment/embedding/
    # recommendation cycle, so a short TTL trades a few seconds of
    # staleness for skipping the DB entirely on repeat loads.
    dashboard_cache_ttl_seconds: int = 30

    # AI reasoning service. AI Vault's assistant is GLM served through
    # OpenRouter; OpenRouter is only the model gateway. `completion_provider`
    # selects the AIGateway's completion adapter (see `ai_gateway/
    # __init__.py`'s `get_ai_gateway()`) — "extractive" is the deterministic
    # no-AI fallback, the default. "openai_compatible" (the OpenRouter chat
    # completions API) only takes effect once BOTH `completion_api_key` and
    # `completion_model_name` are set; otherwise the gateway deliberately
    # keeps the fallback so a half-configured setting can never break every
    # `AIGateway.complete()` caller. The model slug is never defaulted here:
    # it must be copied from openrouter.ai's model catalog, not guessed.
    completion_provider: Literal["extractive", "openai_compatible"] = "extractive"
    completion_api_base_url: str = "https://openrouter.ai/api/v1"
    completion_api_key: str = ""
    completion_model_name: str = ""
    completion_request_timeout_seconds: int = 60
    # Optional OpenRouter app-attribution headers (HTTP-Referer / X-Title).
    completion_http_referer: str = ""
    completion_app_title: str = "AI Vault"
    ai_temperature: float = 0.0
    # Gateway-level resilience for every completion call: bounded retry of
    # transient failures (rate limit / timeout / 5xx), and a per-process
    # request cap protecting the provider quota. 0 disables the cap.
    ai_gateway_max_attempts: int = 3
    ai_gateway_retry_base_delay_seconds: float = 1.0
    ai_gateway_retry_max_delay_seconds: float = 10.0
    ai_gateway_max_requests_per_minute: int = 60

    # AI Storage Assistant (ADR-024) — extends the existing "Ask Vault"
    # conversation system (ConversationService) with deterministic
    # intent-routed tools over Storage Intelligence data. Deliberately
    # reuses `completion_*` above rather than adding a second "which LLM"
    # settings block — OpenRouter (for GLM) is itself an OpenAI-compatible
    # chat-completions endpoint, so pointing `completion_api_base_url` at
    # `https://openrouter.ai/api/v1` with the desired model in
    # `completion_model_name` is all that's needed; no new provider code.
    # There is intentionally no `ai_request_timeout_seconds` — the HTTP
    # timeout is fixed once at provider construction from
    # `completion_request_timeout_seconds` above, and nothing here could
    # enforce a second, unrelated timeout value.
    ai_max_output_tokens: int = 1024
    ai_max_tool_calls: int = 3
    ai_max_context_items: int = 10
    ai_max_history_messages: int = 6
    ai_max_search_results: int = 10
    ai_tool_cache_ttl_seconds: int = 60
    ai_tool_rate_limit_per_minute: int = 10
    ai_max_large_file_threshold_bytes: int = 5 * 1024**4
    ai_max_age_days: int = 3650

    # Archive MVP (ADR — Archive MVP) — object storage for compressed
    # archive packages (see `ObjectStorageClient`). Defaults match
    # docker-compose's local `minio` service; a real deployment points
    # these at an actual S3 bucket/credentials instead. No safe production
    # default for the access/secret key, same reasoning as
    # `connector_encryption_key` above.
    object_storage_endpoint_url: str = "http://minio:9000"
    object_storage_access_key: str = "vault-minio"
    object_storage_secret_key: str = "vault-minio-secret"
    object_storage_bucket: str = "vault-archives"
    object_storage_secure: bool = False
    # Bounds per-file worker memory while building an archive zip — reads are
    # streamed from the storage adapter but still collected into `bytes` here
    # (`read_bounded`) since `zipfile.writestr` needs the whole file at once.
    # A file over this size is skipped (its step fails individually) rather
    # than risking the whole worker process.
    archive_max_file_size_bytes: int = 200 * 1024**2

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    def production_problems(self) -> list[str]:
        """Settings that are acceptable for local development but unsafe in
        production. Messages name the setting, never its value."""
        # Imported here: settings is imported by nearly everything, and only
        # this check needs the Fernet class.
        from cryptography.fernet import Fernet

        problems: list[str] = []
        if (
            self.jwt_secret == _DEFAULT_JWT_SECRET
            or self.jwt_secret.lower().startswith("changeme")
            or len(self.jwt_secret) < 32
        ):
            problems.append(
                "JWT_SECRET is a placeholder or development default, or shorter than 32 characters"
            )
        try:
            Fernet(self.connector_encryption_key.encode())
        except (ValueError, TypeError):
            problems.append("CONNECTOR_ENCRYPTION_KEY is empty or not a valid Fernet key")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if not self.google_client_id or not self.google_client_secret:
            problems.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
        if (self.object_storage_access_key, self.object_storage_secret_key) == _DEFAULT_OBJECT_KEYS:
            problems.append("OBJECT_STORAGE_ACCESS_KEY / SECRET_KEY are the development defaults")
        if any("localhost" in origin or "127.0.0.1" in origin for origin in self.cors_origins_list):
            problems.append("CORS_ALLOW_ORIGINS allows localhost")
        if not self.frontend_url.startswith("https://"):
            problems.append("FRONTEND_URL must use https")
        return problems


_DEFAULT_JWT_SECRET = "changeme-in-env-use-a-real-32-byte-secret"
_DEFAULT_OBJECT_KEYS = ("vault-minio", "vault-minio-secret")


def enforce_production_settings(settings: Settings) -> None:
    """Refuses to start a production process with development defaults. A
    no-op in development and test."""
    if settings.environment != "production":
        return
    problems = settings.production_problems()
    if problems:
        raise RuntimeError(
            "Refusing to start in production with unsafe configuration: " + "; ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
