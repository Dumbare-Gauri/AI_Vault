import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# apps/backend, apps/worker and apps/local-agent are independent Python
# projects (ADR-012), not installed as packages — tests import their
# "app"/"worker"/"aivault_agent" packages directly, so all need to be on
# sys.path regardless of the cwd pytest is invoked from.
for app_dir in ("backend", "worker", "local-agent"):
    path = str(REPO_ROOT / "apps" / app_dir)
    if path not in sys.path:
        sys.path.insert(0, path)


def pytest_configure(config: pytest.Config) -> None:
    """Abort before any test runs if the configured database or Redis could
    be a production one — see `tests/db_safety.py`."""
    from db_safety import check_test_environment, refusal_message

    from vault_shared.settings import get_settings

    settings = get_settings()
    problems = check_test_environment(
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        environment=settings.environment,
    )
    if problems:
        pytest.exit(refusal_message(problems), returncode=2)
