import pytest
from db_safety import ALLOWLIST_ENV_VAR, check_test_environment, refusal_message

_LOCAL_DATABASE = "postgresql+psycopg://vault:vault@localhost:5432/vault"
_LOCAL_REDIS = "redis://localhost:6379/0"


def _check(
    *,
    database_url: str = _LOCAL_DATABASE,
    redis_url: str = _LOCAL_REDIS,
    environment: str = "development",
    environ: dict[str, str] | None = None,
) -> list[str]:
    return check_test_environment(
        database_url=database_url,
        redis_url=redis_url,
        environment=environment,
        environ=environ or {},
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://vault:vault@localhost:5432/vault",
        "postgresql+psycopg://vault:vault@127.0.0.1:5432/vault",
        "postgresql+psycopg://vault:vault@postgres:5432/vault",
        "postgresql+psycopg://vault:vault@postgres:5432/vault_phase0",
        "postgresql+psycopg://vault:vault@host.docker.internal:5434/vault",
    ],
)
def test_local_and_compose_databases_are_accepted(database_url: str) -> None:
    assert _check(database_url=database_url) == []


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://u:p@prod-db.internal:5432/vault",
        "postgresql+psycopg://u:p@vault.abc123.us-east-1.rds.amazonaws.com:5432/vault",
        "postgresql+psycopg://u:p@ep-cool-123.neon.tech/vault",
        "postgresql+psycopg://u:p@10.0.4.12:5432/vault",
        "postgresql+psycopg://u:p@db.example.com:5432/vault",
    ],
)
def test_a_remote_database_is_refused(database_url: str) -> None:
    problems = _check(database_url=database_url)

    assert any("DATABASE_URL" in problem for problem in problems)


@pytest.mark.parametrize("name", ["vault_prod", "production", "vault-live", "PROD_vault"])
def test_a_production_looking_database_name_is_refused_even_on_localhost(name: str) -> None:
    problems = _check(database_url=f"postgresql+psycopg://vault:vault@localhost:5432/{name}")

    assert any("looks like production" in problem for problem in problems)


def test_the_production_environment_is_refused() -> None:
    assert any("production" in problem for problem in _check(environment="production"))


def test_a_remote_redis_is_refused() -> None:
    problems = _check(redis_url="redis://cache.prod.example.com:6379/0")

    assert any("REDIS_URL" in problem for problem in problems)


def test_a_missing_host_fails_closed() -> None:
    assert _check(database_url="postgresql+psycopg:///vault")


def test_a_disposable_remote_test_database_can_be_explicitly_allow_listed() -> None:
    problems = _check(
        database_url="postgresql+psycopg://u:p@ci-db.internal:5432/vault_ci",
        redis_url="redis://ci-db.internal:6379/0",
        environ={ALLOWLIST_ENV_VAR: "ci-db.internal"},
    )

    assert problems == []


def test_the_allow_list_cannot_override_a_production_looking_name() -> None:
    problems = _check(
        database_url="postgresql+psycopg://u:p@ci-db.internal:5432/vault_prod",
        redis_url="redis://ci-db.internal:6379/0",
        environ={ALLOWLIST_ENV_VAR: "ci-db.internal"},
    )

    assert any("looks like production" in problem for problem in problems)


def test_the_allow_list_cannot_override_the_production_environment() -> None:
    problems = _check(environment="production", environ={ALLOWLIST_ENV_VAR: "localhost"})

    assert problems


def test_messages_never_contain_credentials() -> None:
    problems = _check(
        database_url="postgresql+psycopg://admin:Sup3rS3cret!@prod-db.internal:5432/vault_prod",
        redis_url="redis://:RedisS3cret@cache.prod.example.com:6379/0",
    )

    message = refusal_message(problems)
    assert "Sup3rS3cret" not in message
    assert "RedisS3cret" not in message
    assert "admin" not in message
