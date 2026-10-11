"""RateLimiter falls back to a conservative local bucket when Redis is unavailable (POC_03 §7.3)."""

from __future__ import annotations

from reflexo.common.ratelimit import RateLimiter
from reflexo.common.redis import SafeRedis, get_redis, set_redis


async def test_local_fallback_limits_burst() -> None:
    previous = get_redis()
    down = SafeRedis("redis://127.0.0.1:1/0")
    down.set_disabled(True)
    set_redis(down)
    try:
        limiter = RateLimiter(rate_per_s=20, fallback_rate_per_s=5)
        results = [await limiter.try_acquire("conn-1") for _ in range(10)]
        assert results.count(True) == 5  # burst capped at the fallback rate
        assert await limiter.try_acquire("conn-2")  # buckets are per key
    finally:
        set_redis(previous)
