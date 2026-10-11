"""Pipeline X-ray events (POC_01 §6): one row per stage boundary in `pipeline_events`, published to Redis
`xray:{business_id}` for live views. Emission never fails the caller: errors are logged and swallowed."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.common.tracing import current_trace_id

log = get_logger(__name__)


async def emit(
    stage: str,
    status: PipelineEventStatus,
    *,
    business_id: UUID | None = None,
    trace_id: str | None = None,
    duration_ms: int | None = None,
    conversation_id: UUID | None = None,
    turn_id: UUID | None = None,
    **attrs: Any,
) -> None:
    trace = trace_id or current_trace_id() or "untraced"
    event_id = new_id()
    try:
        async with get_db().service_session(ServiceReason.PIPELINE_EVENTS) as s:
            await s.execute(
                text("""INSERT INTO pipeline_events
                        (id, business_id, trace_id, stage, status, duration_ms, conversation_id, turn_id, attrs)
                        VALUES (:id, :b, :t, :stage, CAST(:status AS pipeline_event_status), :d, :c, :turn,
                                CAST(:attrs AS jsonb))"""),
                {
                    "id": event_id,
                    "b": business_id,
                    "t": trace,
                    "stage": stage,
                    "status": str(status),
                    "d": duration_ms,
                    "c": conversation_id,
                    "turn": turn_id,
                    "attrs": json.dumps(attrs, default=str, ensure_ascii=False),
                },
            )
    except Exception as exc:  # observability must never break the pipeline
        log.warning("xray_emit_failed", stage=stage, error=type(exc).__name__)
        return
    if business_id is None:
        return
    message = json.dumps(
        {
            "type": "xray",
            "id": str(event_id),
            "trace_id": trace,
            "stage": stage,
            "status": str(status),
            "duration_ms": duration_ms,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "turn_id": str(turn_id) if turn_id else None,
            "attrs": attrs,
        },
        default=str,
    )
    try:
        await get_redis().call("publish", f"xray:{business_id}", message)
    except RedisUnavailable:
        pass  # SSE backfills from pipeline_events
