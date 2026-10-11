"""Channel policy for outbound sends (POC_03 §9). Checked in order; the first failing rule decides.

1. kill switch → block KILL_SWITCH_ACTIVE
2. opted out → block OPTED_OUT
3. human active (AI/automation origins) → block HUMAN_ACTIVE
4. 24 h window (WhatsApp/Messenger/Instagram) → WhatsApp: template if configured; else block WINDOW_CLOSED
5. automated messages per customer per day → block AUTOMATION_CAP_REACHED
6. quiet hours 21:00–09:00 business time → automation sends deferred (not in demo time-scaling mode)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import get_settings
from reflexo.common.flags import business_killed
from reflexo.common.text import normalize
from reflexo.common.timeutil import to_business_tz, utcnow

AI_ORIGINS = frozenset({"ai_turn", "automation", "voice", "relay", "system"})
WINDOW_PROVIDERS = {"meta_whatsapp", "meta_pages", "meta_instagram"}

OPT_OUT = {"stop", "unsubscribe", "band karo", "mat bhejo", "band kar do", "बंद करो", "मत भेजो"}
OPT_IN = {"start", "shuru karo", "शुरू करो"}


def detect_opt(text: str | None) -> str | None:
    if not text:
        return None
    t = normalize(text)
    if t in OPT_OUT:
        return "out"
    if t in OPT_IN:
        return "in"
    return None


@dataclass(frozen=True)
class PolicyDecision:
    action: str  # allow | block | template | defer
    code: str | None = None
    template: dict[str, Any] | None = None
    defer_until: datetime | None = None


def _opted_out(memory: dict[str, Any], channel: str) -> bool:
    for fact in memory.get("facts", []) if isinstance(memory, dict) else []:
        if fact.get("key") == f"opted_out:{channel}":
            return bool(fact.get("value"))
    return False


async def check(
    s: AsyncSession, *, business_id: UUID, conversation_id: UUID, origin: str, fallback_text: str
) -> PolicyDecision:
    cfg = get_settings()
    if await business_killed(business_id):
        return PolicyDecision("block", "KILL_SWITCH_ACTIVE")
    row = (
        await s.execute(
            text("""
        SELECT c.channel, c.customer_id, c.human_active_until, c.last_inbound_at, cc.provider,
               cust.memory, b.timezone
        FROM conversations c JOIN channel_connections cc ON cc.id = c.channel_connection_id
        JOIN businesses b ON b.id = c.business_id
        LEFT JOIN customers cust ON cust.id = c.customer_id
        WHERE c.id = :id"""),
            {"id": conversation_id},
        )
    ).one()
    now = utcnow()
    if row.customer_id is not None and _opted_out(row.memory or {}, str(row.channel)) and origin != "opt_confirm":
        return PolicyDecision("block", "OPTED_OUT")
    if origin in AI_ORIGINS and row.human_active_until and row.human_active_until > now:
        return PolicyDecision("block", "HUMAN_ACTIVE")
    if str(row.provider) in WINDOW_PROVIDERS and row.customer_id is not None:
        if row.last_inbound_at is None or now - row.last_inbound_at > timedelta(hours=24):
            if str(row.provider) == "meta_whatsapp" and cfg.wa_order_update_template:
                return PolicyDecision(
                    "template",
                    template={"name": cfg.wa_order_update_template, "language": "en", "vars": [fallback_text[:1000]]},
                )
            return PolicyDecision("block", "WINDOW_CLOSED")
    if origin == "automation" and row.customer_id is not None:
        sent_today = (
            await s.execute(
                text("""
            SELECT count(*) FROM outbound_messages o JOIN conversations c2 ON c2.id = o.conversation_id
            WHERE c2.customer_id = :cust AND o.idempotency_key LIKE 'auto:%'
              AND o.created_at > now() - interval '24 hours' AND o.part_index = 0"""),
                {"cust": row.customer_id},
            )
        ).scalar_one()
        if sent_today >= cfg.automation_daily_cap:
            return PolicyDecision("block", "AUTOMATION_CAP_REACHED")
    if origin == "automation" and cfg.demo_time_scale == 1.0:
        local = to_business_tz(now, row.timezone)
        if local.hour >= 21 or local.hour < 9:
            start = local.replace(hour=9, minute=0, second=0, microsecond=0)
            if local.hour >= 21:
                start += timedelta(days=1)
            return PolicyDecision("defer", "QUIET_HOURS", defer_until=start)
    return PolicyDecision("allow")
