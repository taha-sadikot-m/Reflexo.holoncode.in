"""Alembic environment. Migrations run as the owner role (REFLEXO_DATABASE_OWNER_URL); raw SQL, no autogenerate."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from reflexo.common.config import get_settings


def _url() -> str:
    return os.environ.get("REFLEXO_DATABASE_OWNER_URL") or get_settings().database_owner_url


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
