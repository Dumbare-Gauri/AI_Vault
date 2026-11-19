"""Fail-closed guard against running the test suite on a production database.

The integration tests create organizations, users, connectors and jobs, and
some of them run engine code that mutates rows. They read `DATABASE_URL` and
`REDIS_URL` from the environment (or `.env`), so a developer shell or CI job
that happens to have production values exported would otherwise run them
against production. `tests/conftest.py` calls `check_test_environment` before
any test runs and aborts the session if anything looks unsafe.

A URL is accepted only when its host is local or a known development service
name; a hosted database must be allow-listed explicitly, and nothing can
override the production-looking name or `ENVIRONMENT=production` checks.
Messages name the setting and the host or database, never credentials.
"""

import os
import re
from collections.abc import Mapping
from urllib.parse import urlparse

ALLOWLIST_ENV_VAR = "VAULT_TEST_DATABASE_HOST_ALLOWLIST"

LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "postgres", "redis", "host.docker.internal"}
)
_PRODUCTION_NAME = re.compile(r"prod|live", re.IGNORECASE)


def _extra_allowed_hosts(environ: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        host.strip().lower()
        for host in environ.get(ALLOWLIST_ENV_VAR, "").split(",")
        if host.strip()
    )


def check_test_environment(
    *,
    database_url: str,
    redis_url: str,
    environment: str,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Returns human-readable reasons the tests must not run; empty means
    the configured services look like local development or CI ones."""
    environ = os.environ if environ is None else environ
    allowed_hosts = LOCAL_HOSTS | _extra_allowed_hosts(environ)
    problems: list[str] = []

    if environment.lower() == "production":
        problems.append("ENVIRONMENT is 'production'")

    database = urlparse(database_url)
    database_host = (database.hostname or "").lower()
    database_name = database.path.lstrip("/")
    if not database_host or database_host not in allowed_hosts:
        problems.append(
            f"DATABASE_URL points at host {database_host or '<none>'!r}, which is not a local "
            f"or allow-listed test host (add it to {ALLOWLIST_ENV_VAR} only if it is a "
            "disposable test database)"
        )
    if _PRODUCTION_NAME.search(database_name):
        problems.append(
            f"DATABASE_URL names the database {database_name!r}, which looks like production"
        )

    redis_host = (urlparse(redis_url).hostname or "").lower()
    if not redis_host or redis_host not in allowed_hosts:
        problems.append(
            f"REDIS_URL points at host {redis_host or '<none>'!r}, which is not a local "
            "or allow-listed test host"
        )

    return problems


def refusal_message(problems: list[str]) -> str:
    details = "\n  - ".join(problems)
    return (
        "Refusing to run the test suite: the configured services do not look like a "
        f"disposable test environment.\n  - {details}\n"
        "Point DATABASE_URL and REDIS_URL at a local or CI test instance and re-run."
    )
