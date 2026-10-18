"""Database engines and tenant-scoped sessions (POC_02 §6).

Two roles:
* `reflexo_app` — RLS enforced; every transaction sets `app.current_business_id` with `set_config(..., true)`,
  which is transaction-scoped, so a pooled connection never leaks a business id to the next user.
* `reflexo_service` — explicit permissive policies; only for the reasons in `ServiceReason` (POC_02 §6.2).
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from reflexo.common.config import get_settings
from reflexo.common.logging import get_logger

log = get_logger(__name__)


class ServiceReason(StrEnum):
    """Exhaustive list of places allowed to use the service role (POC_02 §6.2)."""

    WEBHOOK_INGRESS = "webhook_ingress"
    INBOUND_RESOLVE = "inbound_resolve"
    QUEUE_CLAIM = "queue_claim"
    INTEGRATION_ROUTING = "integration_routing"
    VOICE_SESSION_START = "voice_session_start"
    VOICE = "voice"  # voice-bridge: call sessions by provider call id, restart recovery (no tenant known yet)
    ADMIN = "admin"
    PIPELINE_EVENTS = "pipeline_events"
    SEED = "seed"
    HEALTH = "health"
    ACCOUNTS = "accounts"  # owner sign-up and log-in (owner_accounts is service-only)
    COSTS = "costs"  # BETA_02 §6 uses 11 and 14: the fx rate, the nightly cost rollup, the benchmark runner


@dataclass
class DbStats:
    """Database work done inside one measured scope (a turn): statements and their time (BETA_12 §2)."""

    queries: int = 0
    ms: float = 0.0


_db_stats: contextvars.ContextVar[DbStats | None] = contextvars.ContextVar("reflexo_db_stats", default=None)


@contextmanager
def measure_db() -> Iterator[DbStats]:
    """Count the statements run (on any engine of this process) inside the block, in this task."""
    stats = DbStats()
    token = _db_stats.set(stats)
    try:
        yield stats
    finally:
        _db_stats.reset(token)


def _count_statements(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: str, params: Any, context: Any, executemany: bool) -> None:
        conn.info.setdefault("reflexo_t0", []).append(time.perf_counter())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn: Any, cursor: Any, statement: str, params: Any, context: Any, executemany: bool) -> None:
        started = conn.info.get("reflexo_t0", []).pop() if conn.info.get("reflexo_t0") else None
        stats = _db_stats.get()
        if stats is not None and started is not None:
            stats.queries += 1
            stats.ms += (time.perf_counter() - started) * 1000


class Database:
    def __init__(self, app_url: str, service_url: str, pool_size: int = 10, statement_timeout_ms: int = 5000) -> None:
        connect_args = {
            "server_settings": {"statement_timeout": str(statement_timeout_ms), "application_name": "reflexo"}
        }
        self.app_engine: AsyncEngine = create_async_engine(
            app_url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, connect_args=connect_args
        )
        self.service_engine: AsyncEngine = create_async_engine(
            service_url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, connect_args=connect_args
        )
        self._app_sessions = async_sessionmaker(self.app_engine, expire_on_commit=False)
        self._service_sessions = async_sessionmaker(self.service_engine, expire_on_commit=False)
        _count_statements(self.app_engine)
        _count_statements(self.service_engine)

    @asynccontextmanager
    async def tenant_session(self, business_id: UUID) -> AsyncIterator[AsyncSession]:
        """One transaction as `reflexo_app`, scoped to `business_id` by RLS. Commits on success."""
        async with self._app_sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_business_id', :b, true)"), {"b": str(business_id)}
            )
            yield session

    @asynccontextmanager
    async def service_session(self, reason: ServiceReason) -> AsyncIterator[AsyncSession]:
        """One transaction as `reflexo_service`. Use only for a listed `ServiceReason`."""
        async with self._service_sessions() as session, session.begin():
            log.debug("service_session", reason=str(reason))
            yield session

    async def warm(self, connections: int) -> None:
        """Open `connections` pooled connections per engine before taking traffic. Cold, the first tenant query
        took ~180 ms and blew the 120 ms speculative-retrieval budget on the first turn after every restart."""

        async def touch(engine: AsyncEngine) -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                await asyncio.sleep(0.05)  # hold it so the next touch opens a new connection

        await asyncio.gather(*(touch(e) for e in (self.app_engine, self.service_engine) for _ in range(connections)))

    async def dispose(self) -> None:
        await self.app_engine.dispose()
        await self.service_engine.dispose()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        s = get_settings()
        _db = Database(s.database_url, s.database_service_url, s.db_pool_size, s.db_statement_timeout_ms)
    return _db


def set_db(db: Database | None) -> None:
    """Override the process-wide database (tests)."""
    global _db
    _db = db


def plain_dsn(sqlalchemy_url: str) -> str:
    """The libpq-style connection string for drivers used directly (asyncpg LISTEN, psycopg for the LangGraph
    saver): no SQLAlchemy dialect prefix, and SQLAlchemy-asyncpg's `ssl=<mode>` as libpq's `sslmode=<mode>` (a
    managed Postgres such as Neon requires TLS; psycopg does not know `ssl`)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    query = [("sslmode", v) if k == "ssl" else (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query)))
