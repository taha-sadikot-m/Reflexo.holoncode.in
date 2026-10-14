"""Calls from a browser (the public demo and the Lab): the same voice agent as a WhatsApp call, without the bridge.

A WhatsApp call reaches LiveKit through the voice-bridge, which plays the caller. In a browser the caller joins the
LiveKit room directly with their microphone, so this only does the bridge's control-plane part: resolve the caller
exactly as a WhatsApp caller (same customer, same chat conversation — so photos sent in the chat during the call and
the recap after it work the same), create the call session, create the room and dispatch the agent with the call
metadata, and hand the browser a short-lived token for that one room. The agent's own shutdown marks the call ended
and sends the recap, as for every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.voice import sessions

log = get_logger(__name__)

BROWSER_HOST = "browser"
TOKEN_TTL = timedelta(minutes=15)


class WebCallUnavailable(Exception):
    pass


@dataclass(frozen=True)
class WebCall:
    call_session_id: UUID
    room: str
    url: str
    token: str


async def _whatsapp_connection(business_id: UUID) -> gateway.ConnectionInfo:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        cid = (
            await s.execute(
                text("""SELECT id FROM channel_connections
                        WHERE business_id = :b AND channel = 'whatsapp' AND status = 'active' LIMIT 1"""),
                {"b": business_id},
            )
        ).scalar_one_or_none()
    conn = await gateway.get_connection(cid) if cid else None
    if conn is None:
        raise WebCallUnavailable("this business has no WhatsApp number to call")
    return conn


async def live_calls() -> int:
    async with get_db().service_session(ServiceReason.VOICE) as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM call_sessions WHERE status IN ('ringing', 'connecting', 'active')")
            )
        ).scalar_one()
    return int(n)


async def start(business_id: UUID, caller_wa_id: str, caller_name: str | None = None) -> WebCall:
    """Start a simulated call from `caller_wa_id` (a simulated customer of this business) and return how the
    browser joins it."""
    from livekit import api as lk_api

    settings = get_settings()
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret.get_secret_value()):
        raise WebCallUnavailable("voice is not configured")
    if await live_calls() >= settings.voice_max_concurrent_calls:
        raise WebCallUnavailable("all lines are busy, please try again in a minute")
    conn = await _whatsapp_connection(business_id)
    async with get_db().tenant_session(business_id) as s:
        who = await gateway.resolve_caller(s, conn, caller_wa_id, caller_name, simulated=True)
    provider_call_id = f"web-{new_id().hex}"
    session, _ = await sessions.create_connecting(
        business_id=business_id,
        provider_call_id=provider_call_id,
        caller_e164="+" + caller_wa_id.lstrip("+"),
        bridge_host=BROWSER_HOST,
        customer_id=who.customer_id,
        member_id=who.member_id,
        conversation_id=who.conversation_id,
    )
    meta = json.dumps(
        {
            "call_session_id": str(session.id),
            "business_id": str(business_id),
            "provider_call_id": provider_call_id,
            "caller_e164": session.caller_e164,
            "role": who.role,
            "path": "browser",
        }
    )
    lk = lk_api.LiveKitAPI(
        settings.livekit_url.replace("wss://", "https://").replace("ws://", "http://"),
        settings.livekit_api_key,
        settings.livekit_api_secret.get_secret_value(),
    )
    try:
        await lk.room.create_room(
            lk_api.CreateRoomRequest(name=session.livekit_room, empty_timeout=60, max_participants=2, metadata=meta)
        )
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name=settings.voice_agent_name, room=session.livekit_room, metadata=meta
            )
        )
    except Exception:
        await sessions.mark_ended(session.id, "setup_failed:livekit", failed=True)
        raise
    finally:
        await lk.aclose()
    await sessions.mark_active(session.id)
    token = (
        lk_api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(f"web-caller-{session.id.hex[:8]}")
        .with_name(caller_name or "Caller")
        .with_ttl(TOKEN_TTL)
        .with_grants(
            lk_api.VideoGrants(room_join=True, room=session.livekit_room, can_publish=True, can_subscribe=True)
        )
        .to_jwt()
    )
    log.info("web_call_started", business_id=str(business_id), call_session_id=str(session.id))
    return WebCall(session.id, session.livekit_room, settings.livekit_public_url or settings.livekit_url, token)
