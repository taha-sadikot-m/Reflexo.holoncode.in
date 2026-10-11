"""Owner notifications (OwnerNotifyPort, POC_05 §4.6; WP-10 adds batching, buttons and the owner timeline).

Recorded once per `dedupe_key` in `owner_notifications`, then delivered on the owner's verified channel:
Telegram first (no 24 h window), else WhatsApp, through the normal outbound path (rendering, policy, idempotency).
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)
CHANNEL_PREFERENCE = ("telegram", "whatsapp")


class OwnerNotifier:
    async def notify(
        self,
        business_id: UUID,
        kind: str,
        text_: str,
        *,
        dedupe_key: str,
        priority: PriorityClass = PriorityClass.P1,
        member_id: UUID | None = None,
        buttons: list[tuple[str, str]] | None = None,
        ref: dict[str, str] | None = None,
    ) -> None:
        """`buttons` = [(option_id, title)] (tap = deterministic owner command); `ref` (e.g. question_id) is stored
        so a swipe-reply to this message can be traced back to what it was about."""
        async with get_db().tenant_session(business_id) as s:
            nid = (
                await s.execute(
                    text("""INSERT INTO owner_notifications (id, business_id, member_id, kind, priority, payload,
                                                             dedupe_key)
                            VALUES (:id, :b, :m, :k, CAST(:p AS priority_class), CAST(:payload AS jsonb), :d)
                            ON CONFLICT (business_id, dedupe_key) DO NOTHING RETURNING id"""),
                    {
                        "id": new_id(),
                        "b": business_id,
                        "m": member_id,
                        "k": kind,
                        "p": str(priority),
                        "payload": json.dumps({"text": text_, **(ref or {})}, ensure_ascii=False),
                        "d": dedupe_key,
                    },
                )
            ).scalar_one_or_none()
            if nid is None:
                return  # already notified (retried job, duplicate webhook)
            bindings = (
                await s.execute(
                    text("""SELECT b.member_id, b.channel, b.external_id FROM owner_channel_bindings b
                            JOIN team_members m ON m.id = b.member_id
                            WHERE b.verified_at IS NOT NULL AND b.revoked_at IS NULL AND NOT b.simulated_only
                              AND m.status = 'active' AND (CAST(:m AS uuid) IS NULL AND m.role = 'owner'
                                                           OR m.id = CAST(:m AS uuid))
                            ORDER BY b.is_primary DESC"""),
                    {"m": member_id},
                )
            ).all()
        target = next((b for ch in CHANNEL_PREFERENCE for b in bindings if str(b.channel) == ch), None)
        if target is None:
            log.warning("owner_has_no_verified_channel", business_id=str(business_id), kind=kind)
            await emit(
                "owner_notify", PipelineEventStatus.SKIP, business_id=business_id, kind=kind, reason="no_channel"
            )
            return
        channel = str(target.channel)
        conn = await self._connection(business_id, channel)
        if conn is None:
            log.warning("owner_channel_not_connected", business_id=str(business_id), channel=channel)
            return
        thread_key = str(target.external_id).lstrip("+") if channel == "whatsapp" else str(target.external_id)
        async with get_db().tenant_session(business_id) as s:
            conv_id = await gateway.member_conversation(s, conn, thread_key, target.member_id)
            await gateway.outbound().enqueue(
                s,
                business_id=business_id,
                conversation_id=conv_id,
                blocks=_blocks(text_, buttons),
                origin="system",
                idempotency_base=f"notify:{nid}",
                priority=priority,
            )
            await s.execute(
                text("""UPDATE owner_notifications SET status = 'sent', delivered_via =
                                    CAST(:ch AS channel_type), delivered_at = now() WHERE id = :id"""),
                {"ch": channel, "id": nid},
            )
        await emit("owner_notify", PipelineEventStatus.OK, business_id=business_id, kind=kind, channel=channel)

    async def _connection(self, business_id: UUID, channel: str) -> gateway.ConnectionInfo | None:
        async with get_db().tenant_session(business_id) as s:
            cid = (
                await s.execute(
                    text("""SELECT id FROM channel_connections WHERE channel = CAST(:ch AS channel_type)
                              AND status = 'active' AND 'owner' = ANY(roles) ORDER BY created_at LIMIT 1"""),
                    {"ch": channel},
                )
            ).scalar_one_or_none()
        return await gateway.get_connection(cid) if cid else None


def _blocks(text_: str, buttons: list[tuple[str, str]] | None) -> list[dict[str, object]]:
    if not buttons:
        return [{"type": "text", "text": text_}]
    options = [{"id": oid[:48], "title": title[:80]} for oid, title in buttons[:10]]
    if len(text_) <= 1024:
        return [{"type": "buttons", "text": text_, "options": options}]
    return [{"type": "text", "text": text_}, {"type": "buttons", "text": "Reply:", "options": options}]


async def notification_for_reply(business_id: UUID, conversation_id: UUID, reply_to: str) -> dict[str, object] | None:
    """The notification (payload incl. its `ref`) that an owner swipe-replied to, if the replied message was one."""
    async with get_db().tenant_session(business_id) as s:
        key = (
            await s.execute(
                text("""SELECT idempotency_key FROM outbound_messages
                        WHERE conversation_id = :c AND provider_message_id = :p AND idempotency_key LIKE 'notify:%'
                        ORDER BY created_at DESC LIMIT 1"""),
                {"c": conversation_id, "p": reply_to},
            )
        ).scalar_one_or_none()
        if key is None:
            return None
        nid = key.split(":")[1]
        row = (
            await s.execute(text("SELECT kind, payload FROM owner_notifications WHERE id = :id"), {"id": nid})
        ).first()
    return {"kind": row.kind, **(row.payload or {})} if row else None
