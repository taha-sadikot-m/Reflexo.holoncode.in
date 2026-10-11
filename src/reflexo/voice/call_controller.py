"""Inbound WhatsApp call control in the voice-bridge (harvested H6 from the old `voice/call_handler.py`).

Kept: per-call lock, "caller hung up during setup" cancellation at every step, best-effort terminate on failure,
accept only after the agent's audio is live. Changed: state in `call_sessions` (idempotent per provider call id,
survives restarts), identity + connection via `gateway.api`, tracing + X-ray per step, a watchdog that ends the
call when the caller's audio stops or the agent leaves (after `end_call`), and restart recovery.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import reflexo.gateway.api as gateway
from reflexo.common.config import get_settings
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.tracing import tracer
from reflexo.common.xray import emit
from reflexo.voice import sessions
from reflexo.voice.meta_calling import MetaCalling
from reflexo.voice.sdp import VoiceSdpError

log = get_logger(__name__)
WATCHDOG_S = 1.0
AGENT_GONE_GRACE_S = 2.0
MAX_CALL_S = 20 * 60


class Bridge(Protocol):
    room_name: str
    caller_audio_ended: asyncio.Event
    metrics: Any

    async def create_sdp_answer(self, sdp_offer: str) -> str: ...

    async def connect_livekit(
        self, url: str, api_key: str, api_secret: str, *, agent_name: str, metadata: dict[str, Any]
    ) -> None: ...

    async def wait_for_agent(self, timeout_s: float) -> bool: ...

    async def agent_present(self) -> bool: ...

    async def close(self) -> None: ...


class CallEnded(Exception):
    """The caller hung up (terminate webhook) while we were still setting the call up."""


@dataclass
class LiveCall:
    call_id: str
    business_id: UUID
    session_id: UUID
    phone_number_id: str
    token: str
    bridge: Bridge
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool = False
    watchdog: asyncio.Task[None] | None = None
    started: float = field(default_factory=time.monotonic)


class CallController:
    def __init__(
        self, host_id: str, *, meta: MetaCalling | None = None, bridge_factory: Callable[[str], Bridge] | None = None
    ) -> None:
        from reflexo.voice.bridge_webrtc import WhatsAppAudioBridge

        self.host_id = host_id
        self.meta = meta or MetaCalling()
        self.bridge_factory = bridge_factory or WhatsAppAudioBridge
        self.live: dict[str, LiveCall] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_end: dict[str, asyncio.Event] = {}

    # ---------------------------------------------------------------- entry point (job / Redis signal)
    async def handle_signal(self, payload: dict[str, Any]) -> None:
        call = payload.get("call") or {}
        call_id = str(call.get("call_id") or "")
        event = str(call.get("event") or "").lower()
        if not call_id:
            log.warning("call_signal_without_id")
            return
        conn = await gateway.get_connection(UUID(str(payload["connection_id"])))
        if conn is None:
            log.warning("call_signal_unknown_connection", call_id=call_id)
            return
        if event == "connect":
            await self._on_connect(conn, call)
        elif event == "terminate":
            await self._on_terminate(call_id, conn.business_id, str((call.get("raw") or {}).get("status") or ""))
        elif event == "permission_reply":
            # automatic call-back permission granted after an inbound call (used by outbound calling, F19)
            perm = call.get("raw") or {}
            await emit(
                "call_event",
                PipelineEventStatus.OK,
                business_id=conn.business_id,
                step="call_permission",
                response=perm.get("response"),
                permanent=perm.get("is_permanent"),
                expires=perm.get("expiration_timestamp"),
            )
        else:
            log.info("call_signal_ignored", call_id=call_id, event_name=event)

    def _lock(self, call_id: str) -> asyncio.Lock:
        return self._locks.setdefault(call_id, asyncio.Lock())

    async def _step(self, business_id: UUID, session_id: UUID | None, name: str, started: float, **attrs: Any) -> None:
        await emit(
            "call_event",
            PipelineEventStatus.OK,
            business_id=business_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            step=name,
            call_session_id=str(session_id) if session_id else None,
            **attrs,
        )

    # ---------------------------------------------------------------- connect
    async def _on_connect(self, conn: gateway.ConnectionInfo, call: dict[str, Any]) -> None:
        call_id = str(call["call_id"])
        settings = get_settings()
        async with self._lock(call_id):
            if call_id in self.live:
                log.info("call_duplicate_connect_ignored", call_id=call_id)
                return
            existing = await sessions.by_provider_call_id(call_id)
            if existing is not None:  # Meta retried the webhook, or a second job copy: act once
                log.info("call_already_handled", call_id=call_id, status=existing.status)
                return
            ended = self._pending_end.setdefault(call_id, asyncio.Event())
            token = gateway.credentials(conn).token
            phone_number_id = conn.external_account_id
            caller_wa = str(call.get("caller_wa_id") or "")
            session_id: UUID | None = None
            bridge: Bridge | None = None
            t0 = time.perf_counter()

            def alive() -> None:
                if ended.is_set():
                    raise CallEnded()

            with tracer().start_as_current_span("voice.call_setup", attributes={"reflexo.call_id": call_id}):
                try:
                    if len(self.live) >= settings.voice_max_concurrent_calls:
                        raise RuntimeError("capacity")
                    async with get_db().tenant_session(conn.business_id) as s:
                        who = await gateway.resolve_caller(s, conn, caller_wa)
                    session, created = await sessions.create_connecting(
                        business_id=conn.business_id,
                        provider_call_id=call_id,
                        caller_e164="+" + caller_wa.lstrip("+"),
                        bridge_host=self.host_id,
                        customer_id=who.customer_id,
                        member_id=who.member_id,
                        conversation_id=who.conversation_id,
                    )
                    if not created:
                        return
                    session_id = session.id
                    await self._step(conn.business_id, session_id, "session", t0, role=who.role)
                    alive()

                    bridge = self.bridge_factory(call_id)
                    # LiveKit setup (room, agent dispatch, caller track) does not depend on the SDP: run it while
                    # the WhatsApp leg gathers ICE and pre-accepts, so the agent is joining during that time.
                    livekit = asyncio.create_task(
                        asyncio.wait_for(
                            bridge.connect_livekit(
                                settings.livekit_url,
                                settings.livekit_api_key,
                                settings.livekit_api_secret.get_secret_value(),
                                agent_name=settings.voice_agent_name,
                                metadata={
                                    "call_session_id": str(session_id),
                                    "business_id": str(conn.business_id),
                                    "provider_call_id": call_id,
                                    "caller_e164": session.caller_e164,
                                    "role": who.role,
                                    "path": "webrtc",
                                },
                            ),
                            10.0,
                        )
                    )
                    try:
                        sdp_answer = await asyncio.wait_for(bridge.create_sdp_answer(str(call.get("sdp") or "")), 8.0)
                        await self._step(conn.business_id, session_id, "sdp_answer", t0)
                        alive()
                        await self.meta.pre_accept(phone_number_id, token, call_id, sdp_answer)
                        await self._step(conn.business_id, session_id, "pre_accept", t0)
                        alive()
                        await livekit
                    except BaseException:
                        livekit.cancel()
                        await asyncio.gather(livekit, return_exceptions=True)
                        raise
                    await self._step(conn.business_id, session_id, "livekit_room", t0, room=bridge.room_name)
                    if not await self._race(bridge.wait_for_agent(settings.voice_agent_join_timeout_s), ended):
                        raise TimeoutError("voice agent did not join in time")
                    await self._step(conn.business_id, session_id, "agent_ready", t0)
                    alive()

                    await self.meta.accept(phone_number_id, token, call_id, sdp_answer)
                    await sessions.mark_active(session_id)
                    live = LiveCall(
                        call_id, conn.business_id, session_id, phone_number_id, token, bridge, ended, accepted=True
                    )
                    live.watchdog = asyncio.create_task(self._watch(live))
                    self.live[call_id] = live
                    await self._step(conn.business_id, session_id, "accepted", t0)
                    log.info("call_accepted", call_id=call_id, setup_ms=int((time.perf_counter() - t0) * 1000))
                except CallEnded:
                    log.info("call_ended_during_setup", call_id=call_id)
                    await self._abort(
                        conn.business_id,
                        call_id,
                        session_id,
                        bridge,
                        token,
                        phone_number_id,
                        "caller_hangup_during_setup",
                        terminate=False,
                    )
                except Exception as exc:
                    reason = "setup_failed:" + ("sdp" if isinstance(exc, VoiceSdpError) else type(exc).__name__)
                    log.warning("call_setup_failed", call_id=call_id, error=str(exc)[:200])
                    await emit(
                        "call_event",
                        PipelineEventStatus.ERROR,
                        business_id=conn.business_id,
                        step="setup_failed",
                        reason=reason,
                    )
                    await self._abort(
                        conn.business_id,
                        call_id,
                        session_id,
                        bridge,
                        token,
                        phone_number_id,
                        reason,
                        terminate=not ended.is_set(),  # tell Meta unless the caller already hung up
                    )
                finally:
                    self._pending_end.pop(call_id, None)

    @staticmethod
    async def _race(coro: Any, ended: asyncio.Event) -> bool:
        main = asyncio.ensure_future(coro)
        stop = asyncio.create_task(ended.wait())
        try:
            done, _ = await asyncio.wait({main, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                raise CallEnded()
            return bool(main.result())
        finally:
            for t in (main, stop):
                t.cancel()
            await asyncio.gather(main, stop, return_exceptions=True)

    async def _abort(
        self,
        business_id: UUID,
        call_id: str,
        session_id: UUID | None,
        bridge: Bridge | None,
        token: str,
        phone_number_id: str,
        reason: str,
        *,
        terminate: bool,
    ) -> None:
        if terminate:
            try:
                await self.meta.terminate(phone_number_id, token, call_id)
            except Exception as exc:  # best effort: the caller may already be gone
                log.info("call_terminate_failed", call_id=call_id, error=type(exc).__name__)
        if bridge is not None:
            await bridge.close()
        if session_id is not None:
            await sessions.mark_ended(session_id, reason, failed=reason.startswith("setup_failed"))

    # ---------------------------------------------------------------- live call
    async def _watch(self, live: LiveCall) -> None:
        """End the call when the caller's media stops, the agent leaves (end_call), or the call runs too long."""
        agent_missing_since: float | None = None
        try:
            while not live.ended.is_set():
                await asyncio.sleep(WATCHDOG_S)
                if live.bridge.caller_audio_ended.is_set():
                    await self.end(live.call_id, "caller_media_ended", terminate=True)
                    return
                if not await live.bridge.agent_present():
                    agent_missing_since = agent_missing_since or time.monotonic()
                    if time.monotonic() - agent_missing_since >= AGENT_GONE_GRACE_S:
                        await self.end(live.call_id, "agent_end", terminate=True)
                        return
                else:
                    agent_missing_since = None
                if time.monotonic() - live.started > MAX_CALL_S:
                    await self.end(live.call_id, "max_duration", terminate=True)
                    return
        except asyncio.CancelledError:
            pass

    async def _on_terminate(self, call_id: str, business_id: UUID, status: str) -> None:
        pending = self._pending_end.get(call_id)
        if pending is not None:
            pending.set()  # setup in progress: it aborts at the next step
        await self.end(
            call_id,
            "caller_hangup" if status.upper() in ("", "COMPLETED") else f"meta_{status.lower()}",
            terminate=False,
        )

    async def end(self, call_id: str, reason: str, *, terminate: bool) -> None:
        """Idempotent teardown of a live call (Graph terminate if we end it, close both legs, update the session)."""
        async with self._lock(call_id):
            live = self.live.pop(call_id, None)
            if live is None:
                return
            live.ended.set()
            if live.watchdog is not None and live.watchdog is not asyncio.current_task():
                live.watchdog.cancel()
            if terminate:
                try:
                    await self.meta.terminate(live.phone_number_id, live.token, call_id)
                except Exception as exc:
                    log.info("call_terminate_failed", call_id=call_id, error=type(exc).__name__)
            await live.bridge.close()
            await sessions.mark_ended(live.session_id, reason)
            await emit(
                "call_event",
                PipelineEventStatus.OK,
                business_id=live.business_id,
                step="ended",
                reason=reason,
                call_session_id=str(live.session_id),
                duration_s=int(time.monotonic() - live.started),
                **live.bridge.metrics.as_attrs(),
            )
        self._locks.pop(call_id, None)
        log.info("call_ended", call_id=call_id, reason=reason)

    # ---------------------------------------------------------------- lifecycle
    async def recover(self) -> int:
        """On start: sessions this bridge owned when it died are ended (`bridge_restart`) and terminated."""
        orphans = await sessions.recover_orphans(self.host_id)
        for o in orphans:
            if not o.provider_call_id or o.connection_id is None:
                continue
            conn = await gateway.get_connection(o.connection_id)
            if conn is None:
                continue
            try:
                await self.meta.terminate(conn.external_account_id, gateway.credentials(conn).token, o.provider_call_id)
            except Exception as exc:
                log.info("recover_terminate_failed", call_id=o.provider_call_id, error=type(exc).__name__)
            await emit(
                "call_event",
                PipelineEventStatus.OK,
                business_id=o.business_id,
                step="recovered",
                reason="bridge_restart",
                call_session_id=str(o.id),
            )
        if orphans:
            log.warning("voice_bridge_recovered_orphans", count=len(orphans))
        return len(orphans)

    async def shutdown(self) -> None:
        for call_id in list(self.live):
            await self.end(call_id, "bridge_shutdown", terminate=True)
