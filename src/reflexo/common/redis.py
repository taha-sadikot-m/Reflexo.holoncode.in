"""Redis access with an explicit "disabled" mode (POC_01 §6: Redis is disposable).

When disabled (Redis outage or the "Redis down" chaos flag), every call raises `RedisUnavailable`, and callers
take their Postgres fallback path. Callers must catch `RedisUnavailable` and never let a Redis failure fail a request.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from reflexo.common.config import get_settings
from reflexo.common.logging import get_logger

log = get_logger(__name__)


class RedisUnavailable(Exception):
    """Redis is down or disabled; use the Postgres fallback."""


class SafeRedis:
    def __init__(self, url: str) -> None:
        self._client = aioredis.from_url(
            url, decode_responses=True, socket_timeout=0.2, socket_connect_timeout=0.5, health_check_interval=10
        )
        self._disabled = False

    @property
    def client(self) -> aioredis.Redis:
        return self._client

    def set_disabled(self, disabled: bool) -> None:
        if disabled != self._disabled:
            log.warning("redis_mode_changed", disabled=disabled)
        self._disabled = disabled

    @property
    def disabled(self) -> bool:
        return self._disabled

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._disabled or await _chaos_disabled():
            raise RedisUnavailable("redis disabled")
        try:
            return await getattr(self._client, method)(*args, **kwargs)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def ping(self) -> bool:
        try:
            return bool(await self.call("ping"))
        except RedisUnavailable:
            return False

    async def close(self) -> None:
        await self._client.aclose()


_redis: SafeRedis | None = None


def get_redis() -> SafeRedis:
    global _redis
    if _redis is None:
        _redis = SafeRedis(get_settings().redis_url)
    return _redis


def set_redis(r: SafeRedis | None) -> None:
    global _redis
    _redis = r


async def _chaos_disabled() -> bool:
    """Chaos "Redis down": every Redis use fails over to its Postgres path (flags are read from Postgres)."""
    from reflexo.common.flags import flag

    try:
        return bool(await flag("redis_disabled", False))
    except Exception:  # the flag store itself must never take Redis users down
        return False
