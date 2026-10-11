"""`call_sessions` persistence (POC_01 §5.3): Postgres is the source of truth for every call; the bridge's
in-memory registry only holds the live WebRTC objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.tracing import current_trace_context

LIVE = ("ringing", "connecting", "active")


@dataclass(frozen=True)
class CallSession:
    id: UUID
    business_id: UUID
    provider_call_id: str | None
    status: str
    customer_id: UUID | None
    member_id: UUID | None
    conversation_id: UUID | None
    caller_e164: str
    livekit_room: str
    bridge_host: str | None
    end_reason: str | None
    connection_id: UUID | None = None  # from the linked WhatsApp conversation


_SELECT = """
    SELECT cs.id, cs.business_id, cs.provider_call_id, cs.status, cs.customer_id, cs.member_id, cs.conversation_id,
           cs.caller_e164, cs.livekit_room, cs.bridge_host, cs.end_reason, c.channel_connection_id AS connection_id
    FROM call_sessions cs LEFT JOIN conversations c ON c.id = cs.conversation_id
"""


def _row(r: Any) -> CallSession:
    return CallSession(
        id=r.id,
        business_id=r.business_id,
        provider_call_id=r.provider_call_id,
        status=str(r.status),
        customer_id=r.customer_id,
        member_id=r.member_id,
        conversation_id=r.conversation_id,
        caller_e164=r.caller_e164,
        livekit_room=r.livekit_room,
        bridge_host=r.bridge_host,
        end_reason=r.end_reason,
        connection_id=r.connection_id,
    )


async def by_provider_call_id(provider_call_id: str) -> CallSession | None:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        r = (await s.execute(text(_SELECT + " WHERE cs.provider_call_id = :p"), {"p": provider_call_id})).first()
    return _row(r) if r else None


async def get(call_session_id: UUID) -> CallSession | None:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        r = (await s.execute(text(_SELECT + " WHERE cs.id = :id"), {"id": call_session_id})).first()
    return _row(r) if r else None


async def create_connecting(
    *,
    business_id: UUID,
    provider_call_id: str,
    caller_e164: str,
    bridge_host: str,
    customer_id: UUID | None,
    member_id: UUID | None,
    conversation_id: UUID | None,
) -> tuple[CallSession, bool]:
    """Insert the session for a new call; a retried `connect` for the same call id returns the existing one."""
    sid = new_id()
    async with get_db().tenant_session(business_id) as s:
        if customer_id is not None:  # a stale live session for this customer (lost hangup) must not block the call
            await s.execute(
                text("""UPDATE call_sessions SET status = 'ended', ended_at = now(), end_reason = 'superseded'
                        WHERE business_id = :b AND customer_id = :c AND status IN ('ringing', 'connecting', 'active')
                          AND provider_call_id IS DISTINCT FROM :p"""),
                {"b": business_id, "c": customer_id, "p": provider_call_id},
            )
        inserted = (
            await s.execute(
                text("""INSERT INTO call_sessions (id, business_id, customer_id, member_id, conversation_id, direction,
                                                   path, provider_call_id, livekit_room, bridge_host, caller_e164,
                                                   status, trace_context)
                        VALUES (:id, :b, :c, :m, :conv, 'inbound', 'webrtc', :p, :room, :host, :caller, 'connecting',
                                CAST(:trace AS jsonb))
                        ON CONFLICT (provider_call_id) WHERE provider_call_id IS NOT NULL DO NOTHING
                        RETURNING id"""),
                {
                    "id": sid,
                    "b": business_id,
                    "c": customer_id,
                    "m": member_id,
                    "conv": conversation_id,
                    "p": provider_call_id,
                    "room": f"call-{provider_call_id}",
                    "host": bridge_host,
                    "caller": caller_e164,
                    "trace": json.dumps(current_trace_context()),
                },
            )
        ).scalar_one_or_none()
    session = await by_provider_call_id(provider_call_id)
    assert session is not None
    return session, inserted is not None


async def mark_active(call_session_id: UUID) -> None:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        await s.execute(
            text("""UPDATE call_sessions SET status = 'active', connected_at = COALESCE(connected_at, now())
                    WHERE id = :id AND status IN ('ringing', 'connecting')"""),
            {"id": call_session_id},
        )


async def mark_ended(call_session_id: UUID, reason: str, *, failed: bool = False) -> bool:
    """End a live session once. Returns False if it had already ended (both legs may report the end)."""
    async with get_db().service_session(ServiceReason.VOICE) as s:
        done = (
            await s.execute(
                text("""UPDATE call_sessions SET status = CAST(:st AS call_status), ended_at = now(), end_reason = :r
                        WHERE id = :id AND status IN ('ringing', 'connecting', 'active') RETURNING id"""),
                {"st": "failed" if failed else "ended", "r": reason[:200], "id": call_session_id},
            )
        ).scalar_one_or_none()
    return done is not None


async def append_transcript(call_session_id: UUID, role: str, text_: str) -> None:
    """Append one turn as it happens, so a crash keeps everything said so far."""
    if not text_.strip():
        return
    turn = json.dumps([{"role": role, "text": text_[:4000]}], ensure_ascii=False)
    async with get_db().service_session(ServiceReason.VOICE) as s:
        await s.execute(
            text("""UPDATE call_sessions SET transcript = transcript || jsonb_set(CAST(:t AS jsonb), '{0,at}',
                                                                                  to_jsonb(now()))
                    WHERE id = :id"""),
            {"t": turn, "id": call_session_id},
        )


async def recover_orphans(bridge_host: str) -> list[CallSession]:
    """Sessions this bridge owned when it died: mark them ended (`bridge_restart`) and return them for terminate."""
    async with get_db().service_session(ServiceReason.VOICE) as s:
        ids = list(
            (
                await s.execute(
                    text("""UPDATE call_sessions SET status = 'ended', ended_at = now(), end_reason = 'bridge_restart'
                            WHERE bridge_host = :h AND status IN ('ringing', 'connecting', 'active') RETURNING id"""),
                    {"h": bridge_host},
                )
            ).scalars()
        )
        if not ids:
            return []
        rows = (await s.execute(text(_SELECT + " WHERE cs.id = ANY(:ids)"), {"ids": ids})).all()
    return [_row(r) for r in rows]
