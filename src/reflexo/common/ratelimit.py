"""Token-bucket rate limiting (POC_03 §7.3).

Primary: an atomic Redis bucket (Lua script) shared by all workers. Fallback when Redis is unavailable:
a conservative per-process bucket (lower rate), so sends continue but cannot burst past provider limits.
"""

from __future__ import annotations

import time

from reflexo.common.redis import RedisUnavailable, get_redis

_LUA = """
local key, rate, burst, now = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1]) or burst
local ts = tonumber(state[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 60)
return allowed
"""


class _LocalBucket:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self.tokens, self.ts = burst, time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimiter:
    def __init__(self, rate_per_s: float, burst: float | None = None, fallback_rate_per_s: float | None = None) -> None:
        self.rate = rate_per_s
        self.burst = burst or rate_per_s
        self.fallback_rate = fallback_rate_per_s or max(1.0, rate_per_s / 4)
        self._local: dict[str, _LocalBucket] = {}

    async def try_acquire(self, key: str) -> bool:
        try:
            allowed = await get_redis().call("eval", _LUA, 1, f"rl:{key}", self.rate, self.burst, time.time())
            return bool(allowed)
        except RedisUnavailable:
            bucket = self._local.setdefault(key, _LocalBucket(self.fallback_rate, self.fallback_rate))
            return bucket.take()
