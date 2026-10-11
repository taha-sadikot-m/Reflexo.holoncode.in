"""Live X-ray stream (POC_07 §4): backfill from `pipeline_events`, then relay Redis `xray:{business_id}`.

If Redis is unavailable (or goes down mid-stream) the stream switches to polling `pipeline_events` every
500 ms and tells the UI (`{"type": "mode", "mode": "polling"}`), so the chaos demo shows the badge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

from reflexo.admin.views import events_since
from reflexo.common.logging import get_logger
from reflexo.common.redis import get_redis

log = get_logger(__name__)
POLL_S = 0.5
KEEPALIVE_S = 15.0


def _sse(data: dict[str, Any]) -> dict[str, str]:
    return {"data": json.dumps(data, default=str, ensure_ascii=False)}


async def xray_stream(business_id: UUID, *, backfill: int = 200) -> AsyncIterator[dict[str, str]]:
    seen: set[str] = set()
    last_at: datetime | None = None

    def track(ev: dict[str, Any]) -> bool:
        nonlocal last_at
        if ev["id"] in seen:
            return False
        seen.add(ev["id"])
        if len(seen) > 5000:
            seen.clear()
        at = ev.get("at")
        if at:
            last_at = max(last_at, datetime.fromisoformat(at)) if last_at else datetime.fromisoformat(at)
        return True

    for ev in await events_since(business_id, None, backfill):
        if track(ev):
            yield _sse(ev)
    yield _sse({"type": "backfill_done"})

    redis = get_redis()
    while True:
        pubsub = None
        try:
            if redis.disabled:
                raise ConnectionError("redis disabled")
            pubsub = redis.client.pubsub()
            await pubsub.subscribe(f"xray:{business_id}")
            yield _sse({"type": "mode", "mode": "live"})
            # catch up on anything emitted while we were subscribing / polling
            for ev in await events_since(business_id, last_at):
                if track(ev):
                    yield _sse(ev)
            idle = 0.0
            while True:
                if redis.disabled:
                    raise ConnectionError("redis disabled")
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    idle += 1.0
                    if idle >= KEEPALIVE_S:
                        idle = 0.0
                        yield {"comment": "keepalive"}
                    continue
                idle = 0.0
                ev = json.loads(msg["data"])
                ev.setdefault("at", None)
                if track(ev):
                    yield _sse(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("xray_stream_polling", reason=type(exc).__name__)
            yield _sse({"type": "mode", "mode": "polling"})
            for _ in range(int(10 / POLL_S)):  # poll for ~10 s, then try Redis again
                for ev in await events_since(business_id, last_at):
                    if track(ev):
                        yield _sse(ev)
                await asyncio.sleep(POLL_S)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()  # type: ignore[no-untyped-call]
                except Exception:  # noqa: S110 - closing a dead connection
                    pass
