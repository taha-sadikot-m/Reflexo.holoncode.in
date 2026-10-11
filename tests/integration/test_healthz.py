"""`/healthz`: Postgres required; Redis reported as degraded when unavailable (Redis is disposable)."""

from __future__ import annotations

import httpx
import pytest

from reflexo.api.main import app
from reflexo.common.db import Database
from reflexo.common.redis import SafeRedis, get_redis, set_redis

pytestmark = pytest.mark.integration


async def test_healthz_ok_with_redis_down(db: Database) -> None:
    previous = get_redis()
    dead_redis = SafeRedis("redis://127.0.0.1:1/0")
    set_redis(dead_redis)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["checks"] == {"postgres": "ok", "redis": "degraded"}
    finally:
        set_redis(previous)
        await dead_redis.close()
