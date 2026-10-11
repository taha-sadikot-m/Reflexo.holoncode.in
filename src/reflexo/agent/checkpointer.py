"""LangGraph checkpointer (POC_04 §4.2): `AsyncPostgresSaver` in the `langgraph` schema, thread_id = turn_id.

A turn that crashes after a model call resumes from its last checkpoint instead of paying for the call again;
side-effecting tools are protected separately by the `tool_effects` ledger. Uses psycopg 3 (the saver's driver),
on its own small pool. On Windows, psycopg's async mode needs the selector event loop (`runtime.selector_loop`).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from reflexo.common.config import get_settings
from reflexo.common.logging import get_logger

log = get_logger(__name__)
_saver: BaseCheckpointSaver[Any] | None = None
_pool: Any = None


def _dsn() -> str:
    url = get_settings().database_service_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def get_checkpointer() -> BaseCheckpointSaver[Any]:
    """Process-wide saver; creates the `langgraph` tables on first use (idempotent)."""
    global _saver, _pool
    if _saver is not None:
        return _saver
    if get_settings().agent_checkpointer == "memory":
        _saver = InMemorySaver()
        return _saver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    _pool = AsyncConnectionPool(
        _dsn(),
        min_size=1,
        max_size=5,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=langgraph",
        },
    )
    await _pool.open()
    saver = AsyncPostgresSaver(_pool)
    await saver.setup()
    _saver = saver
    log.info("checkpointer_ready", kind="postgres", schema="langgraph")
    return _saver


async def delete_thread(thread_id: str) -> None:
    saver = await get_checkpointer()
    await saver.adelete_thread(thread_id)


async def close_checkpointer() -> None:
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver, _pool = None, None
