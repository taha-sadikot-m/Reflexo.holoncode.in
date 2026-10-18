"""Shared fixtures. Integration tests use a throwaway Postgres 16 + pgvector container (testcontainers)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

if sys.platform == "win32":  # psycopg async (LangGraph checkpointer) needs the selector loop on Windows
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REPO = Path(__file__).resolve().parent.parent
OWNER_PW, APP_PW, SERVICE_PW = "owner_test", "app_test", "service_test"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="run tests that call real external providers")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


class PgUrls:
    def __init__(self, host: str, port: int) -> None:
        base = f"{host}:{port}/reflexo"
        self.owner = f"postgresql+asyncpg://reflexo_owner:{OWNER_PW}@{base}"
        self.app = f"postgresql+asyncpg://reflexo_app:{APP_PW}@{base}"
        self.service = f"postgresql+asyncpg://reflexo_service:{SERVICE_PW}@{base}"


def alembic(urls: PgUrls, *args: str) -> None:
    env = {**os.environ, "REFLEXO_DATABASE_OWNER_URL": urls.owner}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, check=True, capture_output=True, text=True
    )


@pytest.fixture(scope="session")
def pg() -> Iterator[PgUrls]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg16", username="reflexo_owner", password=OWNER_PW, dbname="reflexo"
    ) as container:
        roles_sql = (
            f"CREATE ROLE reflexo_app LOGIN PASSWORD '{APP_PW}' NOSUPERUSER NOBYPASSRLS;"
            f"CREATE ROLE reflexo_service LOGIN PASSWORD '{SERVICE_PW}' NOSUPERUSER NOBYPASSRLS;"
            "GRANT CONNECT ON DATABASE reflexo TO reflexo_app, reflexo_service;"
        )
        result = container.exec(
            ["psql", "-U", "reflexo_owner", "-d", "reflexo", "-v", "ON_ERROR_STOP=1", "-c", roles_sql]
        )
        assert result.exit_code == 0, result.output
        urls = PgUrls(container.get_container_host_ip(), int(container.get_exposed_port(5432)))
        alembic(urls, "upgrade", "head")
        yield urls


@pytest.fixture(scope="session")
async def db(pg: PgUrls) -> AsyncIterator[object]:
    from reflexo.common.config import get_settings
    from reflexo.common.db import Database, set_db

    database = Database(pg.app, pg.service, pool_size=5)
    set_db(database)
    # components that open their own connections (LangGraph checkpointer) read the URLs from settings
    saved = {k: os.environ.get(k) for k in ("REFLEXO_DATABASE_URL", "REFLEXO_DATABASE_SERVICE_URL")}
    os.environ["REFLEXO_DATABASE_URL"] = pg.app
    os.environ["REFLEXO_DATABASE_SERVICE_URL"] = pg.service
    get_settings.cache_clear()
    yield database
    from reflexo.agent.checkpointer import close_checkpointer

    await close_checkpointer()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    set_db(None)
    await database.dispose()


TEST_SECRETS = {
    "REFLEXO_META_APP_ID": "111222333",
    "REFLEXO_META_APP_SECRET": "test_meta_app_secret",
    "REFLEXO_META_VERIFY_TOKEN": "test_verify_token",
    "REFLEXO_IG_APP_SECRET": "",
    "REFLEXO_TELEGRAM_WEBHOOK_SECRET": "test_tg_secret",
    "REFLEXO_SIMULATOR_SECRET": "test_sim_secret",
    "REFLEXO_MEDIA_URL_SECRET": "test_media_secret",
    "REFLEXO_WA_ACCESS_TOKEN": "wa-token",
    "REFLEXO_TELEGRAM_BOT_TOKEN": "123:tg-token",
    "REFLEXO_WA_DISPLAY_NUMBER": "+919800000000",
    "REFLEXO_WA_ORDER_UPDATE_TEMPLATE": "order_update",
    "REFLEXO_PUBLIC_BASE_URL": "https://reflexo.test",
    "REFLEXO_ADMIN_USER": "admin",
    "REFLEXO_ADMIN_PASSWORD": "test-admin-password",
    "REFLEXO_ADMIN_SESSION_SECRET": "test-admin-session-secret-0123456789",
    # vault: two test keys (rotation tests switch the active one); never used outside tests
    "REFLEXO_VAULT_KEYS": (
        "k1:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=,k2:ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8="
    ),
    "REFLEXO_VAULT_ACTIVE_KEY_ID": "k1",
    # the rule checker's Linux image (`make policy-check`); tests that need it are marked `slow`
    "REFLEXO_POLICY_CHECK_CMD": "docker run --rm -i reflexo-policy-check:0.1.0",
    # the messages role on its own model, so tests can script it apart from the desks (tests/fake_writer.py)
    "REFLEXO_MODEL_MESSAGES": "gemini-2.5-flash-lite",
}


@pytest.fixture(scope="session", autouse=True)
def _test_settings() -> Iterator[None]:
    """Deterministic secrets for every test (env vars win over any local .env)."""
    from reflexo.common.config import get_settings

    saved = {k: os.environ.get(k) for k in TEST_SECRETS}
    os.environ.update(TEST_SECRETS)
    get_settings.cache_clear()
    from reflexo.vault.api import keyring

    keyring.cache_clear()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _redis_disabled() -> Iterator[None]:
    """Tests run without Redis by default: every Redis call takes the Postgres fallback path instantly."""
    from reflexo.common.redis import SafeRedis, set_redis

    r = SafeRedis("redis://127.0.0.1:1/0")
    r.set_disabled(True)
    set_redis(r)
    yield
    set_redis(None)
