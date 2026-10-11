"""Runtime flags and kill switches (POC_03 §12, specs_v2 D-095).

Read from Postgres (`runtime_flags`, `businesses.kill_switch`) with a 2-second in-process cache, so a change
takes effect everywhere within ~2 s without depending on Redis. Expired flags are ignored.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db

CACHE_TTL_S = 2.0
_flags_cache: tuple[float, dict[str, Any]] | None = None
_kill_cache: dict[UUID, tuple[float, bool]] = {}


async def runtime_flags() -> dict[str, Any]:
    global _flags_cache
    now = time.monotonic()
    if _flags_cache and now - _flags_cache[0] < CACHE_TTL_S:
        return _flags_cache[1]
    async with get_db().service_session(ServiceReason.HEALTH) as s:
        rows = (
            await s.execute(text("SELECT key, value FROM runtime_flags WHERE expires_at IS NULL OR expires_at > now()"))
        ).all()
    flags = {r.key: r.value for r in rows}
    _flags_cache = (now, flags)
    return flags


async def flag(key: str, default: Any = None) -> Any:
    return (await runtime_flags()).get(key, default)


async def business_killed(business_id: UUID) -> bool:
    """Business-level kill switch, or the platform-wide `kill_all` flag."""
    if await flag("kill_all", False):
        return True
    now = time.monotonic()
    cached = _kill_cache.get(business_id)
    if cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    async with get_db().service_session(ServiceReason.HEALTH) as s:
        killed = bool(
            (
                await s.execute(text("SELECT kill_switch FROM businesses WHERE id = :b"), {"b": business_id})
            ).scalar_one_or_none()
        )
    _kill_cache[business_id] = (now, killed)
    return killed


def clear_caches() -> None:
    """Tests and chaos endpoints: make the next read hit Postgres."""
    global _flags_cache
    _flags_cache = None
    _kill_cache.clear()
