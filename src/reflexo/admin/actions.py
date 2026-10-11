"""Admin mutations on conversations (POC_07 §2 Conversations): reply as team, take over, release."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
from reflexo.common.config import get_settings
from reflexo.common.db import get_db
from reflexo.common.ids import new_id


async def reply_as_team(business_id: UUID, conversation_id: UUID, body: str) -> dict[str, str | None]:
    """Send through the normal outbound path (policy, render, idempotency); a human reply also clears the
    needs-attention flag and keeps the AI paused for the takeover window."""
    async with get_db().tenant_session(business_id) as s:
        exists = (
            await s.execute(text("SELECT 1 FROM conversations WHERE id = :c"), {"c": conversation_id})
        ).scalar_one_or_none()
        if exists is None:
            raise LookupError("conversation not found")
        result = await gateway.outbound().enqueue(
            s,
            business_id=business_id,
            conversation_id=conversation_id,
            blocks=[{"type": "text", "text": body}],
            origin="team_reply",
            idempotency_base=f"admin:{new_id()}",
        )
        await s.execute(
            text("""UPDATE conversations SET needs_attention = false,
                           human_active_until = GREATEST(COALESCE(human_active_until, now()),
                                                         now() + make_interval(mins => :m))
                    WHERE id = :c"""),
            {"c": conversation_id, "m": get_settings().human_takeover_min},
        )
    return {
        "message_id": str(result.message_id) if result.message_id else None,
        "status": result.status,
        "code": result.code,
    }


async def set_takeover(business_id: UUID, conversation_id: UUID, active: bool) -> None:
    async with get_db().tenant_session(business_id) as s:
        updated = await s.execute(
            text("""UPDATE conversations SET human_active_until =
                        CASE WHEN :on THEN now() + make_interval(mins => :m) ELSE NULL END,
                        needs_attention = CASE WHEN :on THEN needs_attention ELSE false END
                    WHERE id = :c RETURNING id"""),
            {"on": active, "m": get_settings().human_takeover_min, "c": conversation_id},
        )
        if updated.scalar_one_or_none() is None:
            raise LookupError("conversation not found")
