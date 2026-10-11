"""Inbound processing (InboundProcessorPort; POC_03 §3.2). Order matters and is normative:

parse → connection → [status | call | echo short-circuits] → identity+role → persist message + domain event
(one transaction) → media/typing jobs → link code → opt-out/in → active call → owner command → automation
router → effective customer mode → schedule turn.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    CallRouterPort,
    DomainEventWriter,
    EventRouterPort,
    InboxEvent,
    JobEnqueuer,
    NewDomainEvent,
    OwnerCommandPort,
    RoutableEvent,
    TurnScheduler,
)
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass
from reflexo.common.flags import business_killed
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.common.xray import emit
from reflexo.gateway import identity, policy
from reflexo.gateway.adapters.base import ChannelAdapter
from reflexo.gateway.connections import ConnectionInfo, credentials, find_connection
from reflexo.gateway.normalized import InteractiveReply, NormalizedEvent
from reflexo.gateway.outbound import Outbound
from reflexo.gateway.render import map_reply_to_option
from reflexo.pipeline.api import confirm_by_provider_id

log = get_logger(__name__)
LINK_CODE_RE = re.compile(r"^\s*(LINK-[A-Z0-9]{5})\s*$", re.IGNORECASE)

STATUS_ORDER = ["queued", "sending", "unconfirmed", "sent", "delivered", "read"]

OPT_REPLIES = {
    "out": "You won't receive messages from us any more. Reply START anytime to resume.",
    "in": "Welcome back! You'll receive our messages again.",
}


class GatewayInbound:
    def __init__(self, registry: Registry, adapters: dict[str, ChannelAdapter], outbound: Outbound) -> None:
        self.registry = registry
        self.adapters = adapters
        self.outbound = outbound

    async def process_inbox_event(self, event: InboxEvent) -> None:
        adapter = self.adapters[event.provider]
        started = time.perf_counter()
        events = adapter.parse(event.payload, event.route_key or "")
        await emit(
            "parse",
            PipelineEventStatus.OK,
            duration_ms=int((time.perf_counter() - started) * 1000),
            provider=event.provider,
            events=len(events),
            inbox_id=str(event.id),
        )
        for ev in events:
            await self._process_one(ev, event)

    async def _process_one(self, ev: NormalizedEvent, inbox: InboxEvent) -> None:
        conn = await find_connection(ev.provider, ev.route_key, ev.channel)
        if conn is None:
            log.warning(
                "unknown_connection", provider=str(ev.provider), route_key=ev.route_key, channel=str(ev.channel)
            )
            return
        if ev.kind == "status" and ev.status is not None:
            await self._status(ev)
            return
        if ev.kind == "call":
            await self._call(conn, ev)
            return
        if ev.kind == "echo":
            await self._echo(conn, ev)
            return
        await self._message(conn, ev, inbox)

    # ---------------------------------------------------------------- short-circuit kinds
    async def _status(self, ev: NormalizedEvent) -> None:
        st = ev.status
        assert st is not None
        if st.status in ("sent", "delivered", "read") and await confirm_by_provider_id(
            st.provider_message_id, st.status
        ):
            return
        async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
            await s.execute(
                text("""
                UPDATE messages SET status = CAST(:st AS message_status),
                       delivered_at = CASE WHEN :st IN ('delivered', 'read') THEN COALESCE(delivered_at, now())
                                           ELSE delivered_at END,
                       read_at = CASE WHEN :st = 'read' THEN now() ELSE read_at END,
                       error = CASE WHEN :st = 'failed'
                                    THEN jsonb_build_object('code', CAST(:code AS text), 'message', CAST(:msg AS text))
                                    ELSE error END
                WHERE direction = 'outbound'
                  AND id IN (SELECT message_id FROM outbound_messages WHERE provider_message_id = :pid
                             UNION ALL
                             SELECT id FROM messages WHERE external_message_id = :pid AND direction = 'outbound')
                  -- statuses arrive out of order: only move forward (failed always applies)
                  AND (:st = 'failed'
                       OR COALESCE(array_position(CAST(:order AS text[]), status::text), 0)
                          < array_position(CAST(:order AS text[]), CAST(:st AS text)))"""),
                {
                    "st": st.status,
                    "code": st.error_code,
                    "msg": st.error_message,
                    "pid": st.provider_message_id,
                    "order": STATUS_ORDER,
                },
            )
            await s.execute(
                text("""UPDATE outbound_messages o SET status = 'failed', last_error = :msg
                                    WHERE provider_message_id = :pid AND :st = 'failed'"""),
                {"pid": st.provider_message_id, "st": st.status, "msg": st.error_message},
            )

    async def _call(self, conn: ConnectionInfo, ev: NormalizedEvent) -> None:
        """Hand call signals to the voice-bridge: Redis fast path + durable P0 job (POC_01 §5.3)."""
        assert ev.call is not None
        enqueuer = self.registry.require(JobEnqueuer)  # type: ignore[type-abstract]
        payload = {"business_id": str(conn.business_id), "connection_id": str(conn.id), "call": ev.call.model_dump()}
        await enqueuer.enqueue(
            "voice.call_signal",
            payload,
            business_id=conn.business_id,
            priority=PriorityClass.P0,
            dedupe_key=f"call:{ev.call.call_id}:{ev.call.event}",
        )
        try:
            await get_redis().call("publish", "ctl:voice", json.dumps(payload))
        except RedisUnavailable:
            pass

    async def _echo(self, conn: ConnectionInfo, ev: NormalizedEvent) -> None:
        """A message sent from the business inbox by a human → takeover. Our own API sends are ignored."""
        if ev.echo_app_id and ev.echo_app_id == get_settings().meta_app_id:
            return
        async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
            ours = (
                await s.execute(
                    text("SELECT 1 FROM outbound_messages WHERE provider_message_id = :p"),
                    {"p": ev.external_message_id},
                )
            ).scalar_one_or_none()
            if ours:
                return
            conv = (
                await s.execute(
                    text("""
                SELECT id FROM conversations WHERE channel_connection_id = :cc AND thread_key = :tk"""),
                    {"cc": conn.id, "tk": ev.thread_key},
                )
            ).scalar_one_or_none()
            if conv is None:
                return
            minutes = get_settings().human_takeover_min
            await s.execute(
                text("""
                INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                      channel_connection_id,
                                      external_message_id, content_text, status)
                VALUES (:id, :b, :c, 'outbound', 'team', CAST(:ch AS channel_type), :cc, :ext, :t, 'sent')
                ON CONFLICT DO NOTHING"""),
                {
                    "id": new_id(),
                    "b": conn.business_id,
                    "c": conv,
                    "ch": conn.channel,
                    "cc": conn.id,
                    "ext": ev.external_message_id,
                    "t": ev.text or "[message from business inbox]",
                },
            )
            await s.execute(
                text("""UPDATE conversations SET human_active_until = now() + make_interval(mins => :m)
                                    WHERE id = :c"""),
                {"m": minutes, "c": conv},
            )
        await emit("human_takeover", PipelineEventStatus.OK, business_id=conn.business_id, conversation_id=conv)

    # ---------------------------------------------------------------- messages
    async def _message(self, conn: ConnectionInfo, ev: NormalizedEvent, inbox: InboxEvent) -> None:
        t0 = time.perf_counter()
        writer = self.registry.require(DomainEventWriter)  # type: ignore[type-abstract]
        async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
            sender = await identity.resolve(s, conn, ev, inbox.simulated)
            interactive = ev.interactive
            if interactive is None and ev.text and sender.option_map.get("options"):
                options = cast(list[dict[str, Any]], sender.option_map["options"])
                hit = map_reply_to_option(ev.text, options)
                if hit is not None:
                    interactive = InteractiveReply(option_id=hit["option_id"], title=hit["title"])
            message_id = new_id()
            inserted = (
                await s.execute(
                    text("""
                INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                      channel_connection_id,
                                      external_message_id, content_text, interactive_reply, status)
                VALUES (:id, :b, :c, 'inbound', CAST(:a AS message_author), CAST(:ch AS channel_type), :cc, :ext,
                        :t, CAST(:inter AS jsonb), 'received')
                ON CONFLICT (channel_connection_id, external_message_id) WHERE external_message_id IS NOT NULL
                DO NOTHING RETURNING id"""),
                    {
                        "id": message_id,
                        "b": conn.business_id,
                        "c": sender.conversation_id,
                        "a": "customer" if sender.role == "customer" else sender.role,
                        "ch": conn.channel,
                        "cc": conn.id,
                        "ext": ev.external_message_id,
                        "t": ev.text or (f"[{ev.media[0].kind}]" if ev.media else "[message]"),
                        "inter": json.dumps(interactive.model_dump()) if interactive else None,
                    },
                )
            ).scalar_one_or_none()
            if inserted is None:
                log.info("duplicate_message_ignored", external_id=ev.external_message_id)
                return
            await s.execute(
                text("""UPDATE conversations SET last_inbound_at = now(), status = 'open' WHERE id = :c"""),
                {"c": sender.conversation_id},
            )
            event_type = "comment.received" if ev.kind == "comment" else "message.received"
            domain_event_id = None
            if sender.role == "customer":
                domain_event_id = await writer.append(
                    s,
                    NewDomainEvent(
                        business_id=conn.business_id,
                        type=event_type,
                        source="gateway",
                        correlation_id=inbox.id,
                        customer_id=sender.customer_id,
                        conversation_id=sender.conversation_id,
                        payload={
                            "message_id": str(message_id),
                            "channel": conn.channel,
                            "text": ev.text,
                            "option_id": interactive.option_id if interactive else None,
                            "post_id": ev.comment.post_id if ev.comment else None,
                            "comment_id": ev.comment.comment_id if ev.comment else None,
                        },
                    ),
                )
        await emit(
            "identity",
            PipelineEventStatus.OK,
            business_id=conn.business_id,
            conversation_id=sender.conversation_id,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            role=sender.role,
            new_customer=sender.is_new_customer,
            simulated=sender.conversation_simulated,
        )

        await self._side_jobs(conn, ev, sender, message_id, inbox.simulated)

        if sender.role == "customer" and sender.customer_id is not None:
            if await self._link_code(conn, ev, sender.customer_id, sender.conversation_id, message_id):
                return
            if await self._opt(conn, ev, sender.customer_id, sender.conversation_id, message_id):
                return
            call_router = self.registry.optional(CallRouterPort)  # type: ignore[type-abstract]
            if call_router is not None:
                routed = await call_router.deliver_if_active(
                    conn.business_id, sender.customer_id, sender.conversation_id, message_id
                )
                await emit(
                    "call_route",
                    PipelineEventStatus.OK,
                    business_id=conn.business_id,
                    conversation_id=sender.conversation_id,
                    result=routed,
                )
                if routed in ("delivered", "buffered"):
                    return
            router = self.registry.optional(EventRouterPort)  # type: ignore[type-abstract]
            if router is not None:
                decision = await router.route_inbound(
                    RoutableEvent(
                        type=event_type,
                        business_id=conn.business_id,
                        customer_id=sender.customer_id,
                        conversation_id=sender.conversation_id,
                        message_id=message_id,
                        channel=conn.channel,
                        text=ev.text,
                        interactive_option_id=interactive.option_id if interactive else None,
                        domain_event_id=domain_event_id,
                        post_id=ev.comment.post_id if ev.comment else None,
                        comment_id=ev.comment.comment_id if ev.comment else None,
                    )
                )
                await emit(
                    "automation_route",
                    PipelineEventStatus.OK,
                    business_id=conn.business_id,
                    conversation_id=sender.conversation_id,
                    result=decision,
                )
                if decision == "consumed":
                    return
            if not await self._customer_turn_allowed(conn, sender.conversation_id):
                await emit(
                    "turn_scheduled",
                    PipelineEventStatus.SKIP,
                    business_id=conn.business_id,
                    conversation_id=sender.conversation_id,
                    reason="mode",
                )
                return
            await self._schedule(conn, sender.conversation_id, message_id, Desk.CUSTOMER, PriorityClass.P0, ev)
            return

        # owner / team
        assert sender.member_id is not None
        commands = self.registry.optional(OwnerCommandPort)  # type: ignore[type-abstract]
        if commands is not None and await commands.try_handle(
            conn.business_id,
            sender.member_id,
            sender.conversation_id,
            message_id,
            ev.text,
            interactive.option_id if interactive else None,
        ):
            await emit(
                "owner_command",
                PipelineEventStatus.OK,
                business_id=conn.business_id,
                conversation_id=sender.conversation_id,
            )
            return
        await self._schedule(conn, sender.conversation_id, message_id, Desk.OWNER, PriorityClass.P1, ev)

    async def _side_jobs(
        self,
        conn: ConnectionInfo,
        ev: NormalizedEvent,
        sender: identity.ResolvedSender,
        message_id: UUID,
        simulated: bool,
    ) -> None:
        enqueuer = self.registry.require(JobEnqueuer)  # type: ignore[type-abstract]
        for i, media in enumerate(ev.media):
            await enqueuer.enqueue(
                "media.fetch",
                {
                    "business_id": str(conn.business_id),
                    "connection_id": str(conn.id),
                    "message_id": str(message_id),
                    "media": media.model_dump(),
                    "owner_member_id": str(sender.member_id) if sender.member_id else None,
                },
                business_id=conn.business_id,
                priority=PriorityClass.P0,
                dedupe_key=f"media:{message_id}:{i}",
            )
        if not (simulated or sender.conversation_simulated) and conn.provider != "simulator":
            await enqueuer.enqueue(
                "gateway.typing",
                {
                    "connection_id": str(conn.id),
                    "recipient": ev.thread_key,
                    "external_message_id": ev.external_message_id,
                },
                business_id=conn.business_id,
                priority=PriorityClass.P0,
                dedupe_key=f"typing:{sender.conversation_id}",
            )
        cq = ev.raw.get("callback_query_id") if isinstance(ev.raw, dict) else None
        if cq and conn.provider == "telegram":
            ack = getattr(self.adapters["telegram"], "acknowledge_interaction", None)
            if ack is not None:
                try:
                    await ack(credentials(conn), cq)
                except Exception as exc:  # best effort UI ack
                    log.info("callback_ack_failed", error=type(exc).__name__)

    async def _link_code(
        self, conn: ConnectionInfo, ev: NormalizedEvent, customer_id: UUID, conversation_id: UUID, message_id: UUID
    ) -> bool:
        if conn.channel != "whatsapp":
            return False
        match = LINK_CODE_RE.match(ev.text or "") or LINK_CODE_RE.match(ev.referral_code or "")
        if not match:
            return False
        async with get_db().tenant_session(conn.business_id) as s:
            outcome = await identity.consume_link_code(s, conn.business_id, match.group(1), customer_id)
            reply = {
                "linked": "Done! ✅ I'll continue here on WhatsApp — I have your earlier chat with me.",
                "already_linked": "You're all set — we're already connected here. 🙂",
                "expired": "That code has expired. Please ask for a new one in the original chat.",
                "unknown": "I couldn't recognise that code. Please check and try again.",
            }[outcome.status]
            await self.outbound.enqueue(
                s,
                business_id=conn.business_id,
                conversation_id=conversation_id,
                blocks=[{"type": "text", "text": reply}],
                origin="system",
                idempotency_base=f"link:{message_id}",
            )
        await emit(
            "link_code",
            PipelineEventStatus.OK,
            business_id=conn.business_id,
            conversation_id=conversation_id,
            outcome=outcome.status,
        )
        return True

    async def _opt(
        self, conn: ConnectionInfo, ev: NormalizedEvent, customer_id: UUID, conversation_id: UUID, message_id: UUID
    ) -> bool:
        choice = policy.detect_opt(ev.text)
        if choice is None:
            return False
        async with get_db().tenant_session(conn.business_id) as s:
            await identity.record_opt(s, customer_id, conn.channel, opted_out=choice == "out")
            await self.outbound.enqueue(
                s,
                business_id=conn.business_id,
                conversation_id=conversation_id,
                blocks=[{"type": "text", "text": OPT_REPLIES[choice]}],
                origin="opt_confirm",
                idempotency_base=f"opt:{message_id}",
            )
        return True

    async def _customer_turn_allowed(self, conn: ConnectionInfo, conversation_id: UUID) -> bool:
        if conn.business_status != "active" or conn.customer_mode == "off" or "customer" not in conn.roles:
            return False
        if await business_killed(conn.business_id):
            return False
        async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
            human = (
                await s.execute(
                    text("""SELECT human_active_until > now() FROM conversations WHERE id = :c"""),
                    {"c": conversation_id},
                )
            ).scalar_one()
        return not human

    async def _schedule(
        self,
        conn: ConnectionInfo,
        conversation_id: UUID,
        message_id: UUID,
        desk: Desk,
        priority: PriorityClass,
        ev: NormalizedEvent,
    ) -> None:
        scheduler = self.registry.require(TurnScheduler)  # type: ignore[type-abstract]
        async with get_db().tenant_session(conn.business_id) as s:
            turn_id = await scheduler.schedule(
                s,
                business_id=conn.business_id,
                conversation_id=conversation_id,
                message_id=message_id,
                desk=desk,
                priority=priority,
                ends_thought=ev.ends_thought,
            )
        await emit(
            "turn_scheduled",
            PipelineEventStatus.OK,
            business_id=conn.business_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            desk=str(desk),
        )
