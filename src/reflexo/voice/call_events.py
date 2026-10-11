"""In-call chat events (POC_04 §10.5, POC_01 §5.3): WhatsApp chat messages sent during a live call go to the voice
agent instead of the chat agent.

Postgres `call_events` is the source of truth; Redis `call:{call_session_id}` only wakes the agent (it also polls,
so a Redis outage costs latency, not messages). A message that arrives while the call is still connecting is
buffered and picked up when the agent starts (catch-up on pending events).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.redis import get_redis
from reflexo.common.xray import emit

log = get_logger(__name__)
KINDS = {"text", "image", "document", "audio", "location"}
CONNECTING = ("ringing", "connecting")


def channel(call_session_id: UUID) -> str:
    return f"call:{call_session_id}"


@dataclass(frozen=True)
class CallEvent:
    id: UUID
    call_session_id: UUID
    kind: str
    message_id: UUID | None
    text: str
    payload: dict[str, Any]


class CallRouter:
    """CallRouterPort: route a customer's chat message into their live call, if any."""

    async def deliver_if_active(
        self, business_id: UUID, customer_id: UUID, conversation_id: UUID, message_id: UUID, kind: str = "text"
    ) -> str:
        async with get_db().tenant_session(business_id) as s:
            live = (
                await s.execute(
                    text("""SELECT id, status FROM call_sessions
                            WHERE status IN ('ringing', 'connecting', 'active')
                              AND (conversation_id = :conv OR customer_id = :cust)
                            ORDER BY created_at DESC LIMIT 1"""),
                    {"conv": conversation_id, "cust": customer_id},
                )
            ).first()
            if live is None:
                return "none"
            body = (
                await s.execute(text("SELECT content_text FROM messages WHERE id = :m"), {"m": message_id})
            ).scalar_one_or_none()
            event_id = new_id()
            await s.execute(
                text("""INSERT INTO call_events (id, business_id, call_session_id, kind, message_id, payload)
                        VALUES (:id, :b, :c, CAST(:k AS call_event_kind), :m, CAST(:p AS jsonb))"""),
                {
                    "id": event_id,
                    "b": business_id,
                    "c": live.id,
                    "k": kind if kind in KINDS else "text",
                    "m": message_id,
                    "p": json.dumps({"text": body or ""}, ensure_ascii=False),
                },
            )
            await s.execute(
                text("UPDATE messages SET call_session_id = :c WHERE id = :m"), {"c": live.id, "m": message_id}
            )
        try:
            await get_redis().call("publish", channel(live.id), str(event_id))
        except Exception:  # Redis only wakes the agent early; it also polls call_events
            log.info("call_event_publish_failed", call_session_id=str(live.id))
        decision = "buffered" if str(live.status) in CONNECTING else "delivered"
        await emit(
            "call_event",
            PipelineEventStatus.OK,
            business_id=business_id,
            conversation_id=conversation_id,
            call_session_id=str(live.id),
            kind=kind,
            result=decision,
        )
        return decision


async def claim_pending(call_session_id: UUID) -> list[CallEvent]:
    """pending → delivered, oldest first; each event is handed out once (multiple pollers are safe)."""
    async with get_db().service_session(ServiceReason.VOICE) as s:
        rows = (
            await s.execute(
                text("""UPDATE call_events SET status = 'delivered'
                        WHERE id IN (SELECT id FROM call_events WHERE call_session_id = :c AND status = 'pending'
                                     ORDER BY created_at FOR UPDATE SKIP LOCKED)
                        RETURNING id, call_session_id, kind, message_id, payload, created_at"""),
                {"c": call_session_id},
            )
        ).all()
    return [
        CallEvent(
            r.id,
            r.call_session_id,
            str(r.kind),
            r.message_id,
            str((r.payload or {}).get("text") or ""),
            dict(r.payload or {}),
        )
        for r in sorted(rows, key=lambda r: r.created_at)
    ]


async def mark(event_id: UUID, status: str, payload: dict[str, Any] | None = None) -> None:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        await s.execute(
            text("""UPDATE call_events SET status = CAST(:st AS call_event_status),
                           payload = payload || CAST(:p AS jsonb), processed_at = now() WHERE id = :id"""),
            {"st": status, "p": json.dumps(payload or {}, ensure_ascii=False, default=str), "id": event_id},
        )


async def wait_media(message_id: UUID, *, timeout_s: float = 8.0) -> UUID | None:
    """The media object of an in-call photo/document; `media.fetch` may still be downloading it."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        async with get_db().service_session(ServiceReason.VOICE) as s:
            attachments = (
                await s.execute(text("SELECT attachments FROM messages WHERE id = :m"), {"m": message_id})
            ).scalar_one_or_none()
        for a in attachments or []:
            if a.get("media_object_id"):
                return UUID(str(a["media_object_id"]))
        if asyncio.get_running_loop().time() > deadline:
            return None
        await asyncio.sleep(0.4)


async def subscribe(call_session_id: UUID, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Sets `wake` whenever Redis announces an event for this call (best effort; the loop also polls)."""
    try:
        pubsub = get_redis().client.pubsub()
        await pubsub.subscribe(channel(call_session_id))
    except Exception:
        log.info("call_event_subscribe_failed", call_session_id=str(call_session_id))
        return
    try:
        while not stop.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                wake.set()
    except Exception:
        log.info("call_event_subscription_lost", call_session_id=str(call_session_id))
    finally:
        with contextlib.suppress(Exception):  # the call is over; nothing to recover
            await pubsub.unsubscribe(channel(call_session_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
