"""Database engines and tenant-scoped sessions (POC_02 §6).

Two roles:
* `reflexo_app` — RLS enforced; every transaction sets `app.current_business_id` with `set_config(..., true)`,
  which is transaction-scoped, so a pooled connection never leaks a business id to the next user.
* `reflexo_service` — explicit permissive policies; only for the reasons in `ServiceReason` (POC_02 §6.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
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
