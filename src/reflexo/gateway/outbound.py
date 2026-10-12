"""Outbound: policy → render → messages + send log (enqueue), and the send step (OutboundSenderPort).

`enqueue` runs inside the caller's transaction (e.g. `TurnHandle.commit_reply`), so the reply rows commit
atomically with the turn. Idempotent: the same `idempotency_base` never creates a second message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.blocks import ContentBlock
from reflexo.common.contracts import BlockHydrator, OutboundRow, OwnerNotifyPort, SendResult
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.common.tracing import current_trace_context
from reflexo.gateway import policy
from reflexo.gateway.adapters.base import ChannelAdapter, RenderContext
from reflexo.gateway.adapters.simulator import deliver_to_lab
from reflexo.gateway.connections import credentials, get_connection
from reflexo.gateway.media import MediaSigner
from reflexo.gateway.render import Renderer

log = get_logger(__name__)
BLOCKS = TypeAdapter(list[ContentBlock])

AUTHOR_BY_ORIGIN = {
    "ai_turn": "ai",
    "voice": "ai",
    "relay": "ai",
    "automation": "system",
    "system": "system",
    "opt_confirm": "system",
    "owner_command": "owner",
    "team_reply": "team",
}


@dataclass(frozen=True)
class EnqueueResult:
    message_id: UUID | None
    part_ids: list[UUID]
    status: str  # queued | blocked | deferred | duplicate
    code: str | None = None


class Outbound:
    def __init__(self, registry: Registry, adapters: dict[str, ChannelAdapter]) -> None:
        self.registry = registry
        self.adapters = adapters
        self.signer = MediaSigner()

    async def enqueue(
        self,
        s: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        blocks: list[ContentBlock] | list[dict[str, Any]],
        origin: str,
        idempotency_base: str,
        priority: PriorityClass = PriorityClass.P0,
        turn_id: UUID | None = None,
        call_session_id: UUID | None = None,
    ) -> EnqueueResult:
        parsed: list[ContentBlock] = BLOCKS.validate_python(blocks)
        existing = (
            await s.execute(
                text("SELECT message_id FROM outbound_messages WHERE idempotency_key = :k"),
                {"k": f"{idempotency_base}:0"},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return EnqueueResult(existing, [], "duplicate")
        conv = (
            await s.execute(
                text("""
            SELECT c.thread_key, c.channel, c.channel_connection_id, cc.provider, c.participant_role
            FROM conversations c JOIN channel_connections cc ON cc.id = c.channel_connection_id
            WHERE c.id = :id"""),
                {"id": conversation_id},
            )
        ).one()
        provider = str(conv.provider)
        adapter = self.adapters[provider]
        renderer = Renderer(
            adapter.manifest,
            self.registry.optional(BlockHydrator),  # type: ignore[type-abstract]
            self.signer,
        )
        rendered = await renderer.render(business_id, parsed)

        decision = await policy.check(
            s,
            business_id=business_id,
            conversation_id=conversation_id,
            origin=origin,
            fallback_text=rendered.text_fallback,
        )
        message_id = new_id()
        author = AUTHOR_BY_ORIGIN.get(origin, "system")
        blocks_json = json.dumps([b.model_dump(mode="json") for b in parsed])
        if decision.action == "block":
            await s.execute(
                text("""
                INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                      channel_connection_id,
                                      content_text, content_blocks, status, turn_id, call_session_id, error)
                VALUES (:id, :b, :c, 'outbound', CAST(:a AS message_author), CAST(:ch AS channel_type), :cc, :t,
                        CAST(:blocks AS jsonb), 'blocked', :turn, :call,
                        jsonb_build_object('code', CAST(:code AS text)))"""),
                {
                    "id": message_id,
                    "b": business_id,
                    "c": conversation_id,
                    "a": author,
                    "ch": str(conv.channel),
                    "cc": conv.channel_connection_id,
                    "t": rendered.text_fallback,
                    "blocks": blocks_json,
                    "turn": turn_id,
                    "call": call_session_id,
                    "code": decision.code,
                },
            )
            log.info("outbound_blocked", code=decision.code, conversation_id=str(conversation_id))
            return EnqueueResult(message_id, [], "blocked", decision.code)

        is_comment = str(conv.channel) in ("instagram_comment", "facebook_comment")
        ctx = RenderContext(
            recipient=str(conv.thread_key),
            comment_id=str(conv.thread_key) if is_comment else None,
            template=decision.template,
        )
        payloads = adapter.to_payloads(rendered.parts, ctx)
        await s.execute(
            text("""
            INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                      channel_connection_id,
                                  content_text, content_blocks, status, turn_id, call_session_id)
            VALUES (:id, :b, :c, 'outbound', CAST(:a AS message_author), CAST(:ch AS channel_type), :cc, :t,
                    CAST(:blocks AS jsonb), 'queued', :turn, :call)"""),
            {
                "id": message_id,
                "b": business_id,
                "c": conversation_id,
                "a": author,
                "ch": str(conv.channel),
                "cc": conv.channel_connection_id,
                "t": rendered.text_fallback,
                "blocks": blocks_json,
                "turn": turn_id,
                "call": call_session_id,
            },
        )
        part_ids: list[UUID] = []
        not_before = decision.defer_until
        trace = json.dumps(current_trace_context())  # sends continue the trace that produced them
        for i, payload in enumerate(payloads):
            pid = new_id()
            part_ids.append(pid)
            await s.execute(
                text("""
                INSERT INTO outbound_messages (id, business_id, message_id, conversation_id, channel_connection_id,
                                               part_index, idempotency_key, rendered_payload, priority, next_attempt_at,
                                               trace_context)
                VALUES (:id, :b, :m, :c, :cc, :i, :k, CAST(:p AS jsonb), CAST(:prio AS priority_class),
                        COALESCE(:nb, now()), CAST(:trace AS jsonb))
                ON CONFLICT (idempotency_key) DO NOTHING"""),
                {
                    "id": pid,
                    "b": business_id,
                    "m": message_id,
                    "c": conversation_id,
                    "cc": conv.channel_connection_id,
                    "i": i,
                    "k": f"{idempotency_base}:{i}",
                    "p": json.dumps(payload, ensure_ascii=False),
                    "prio": str(priority),
                    "nb": not_before,
                    "trace": trace,
                },
            )
        if rendered.options:
            option_map = {"message_id": str(message_id), "options": rendered.options}
            await s.execute(
                text("UPDATE conversations SET option_map = CAST(:o AS jsonb), last_outbound_at = now() WHERE id = :c"),
                {"o": json.dumps(option_map), "c": conversation_id},
            )
            try:
                await get_redis().call("set", f"opts:{conversation_id}", json.dumps(option_map), ex=86400)
            except RedisUnavailable:
                pass  # conversations.option_map is the durable copy
        await s.execute(text("SELECT pg_notify('q_outbound', '')"))
        return EnqueueResult(message_id, part_ids, "deferred" if not_before else "queued")

    # ---------------------------------------------------------------- OutboundSenderPort
    async def send(self, row: OutboundRow) -> SendResult:
        conn = await get_connection(row.channel_connection_id)
        if conn is None:
            raise LookupError(f"connection {row.channel_connection_id} not found")
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            simulated = bool(
                (
                    await s.execute(
                        text("SELECT simulated FROM conversations WHERE id = :id"), {"id": row.conversation_id}
                    )
                ).scalar_one()
            )
        if simulated or conn.provider == "simulator":
            return await deliver_to_lab(
                row.rendered_payload,
                business_id=row.business_id,
                conversation_id=row.conversation_id,
                provider=conn.provider,
            )
        return await self.adapters[conn.provider].send(row.rendered_payload, credentials(conn))

    async def on_final_failure(self, row: OutboundRow, result: SendResult) -> None:
        notifier = self.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
        log.warning("outbound_final_failure", outbound_id=str(row.id), code=result.error_code)
        if notifier is not None:
            await notifier.notify(
                row.business_id,
                "delivery_failed",
                f"A reply could not be delivered ({result.error_code}).",
                dedupe_key=f"delivery_failed:{row.message_id}",
                about_conversation=row.conversation_id,
            )
