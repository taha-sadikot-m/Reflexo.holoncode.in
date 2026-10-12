"""Admin mutations (POC_07 §2): reply as team, take over, release; the demo reset (Settings)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
from reflexo.admin import seeding
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import WorkflowPort
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


_reset_lock = asyncio.Lock()


async def demo_reset(mode: str) -> dict[str, Any]:
    """The Settings page's reset button = `make demo-reset [FULL=1]`: stop the demo businesses' workflows, wipe,
    reseed. Business ids change; one reset at a time."""
    if mode not in ("onboarding", "full"):
        raise ValueError("mode must be onboarding or full")
    if _reset_lock.locked():
        raise RuntimeError("a reset is already running")
    async with _reset_lock:
        report = await seeding.reset(
            get_db(),
            "full" if mode == "full" else "onboarding",
            workflows=ports.registry.optional(WorkflowPort),  # type: ignore[type-abstract]
        )
    gateway.clear_connection_cache()  # this process routes to the new rows at once (workers: within the TTL)
    return {
        "businesses": {slug: str(bid) for slug, bid in report.businesses.items()},
        "seconds": round(report.seconds, 1),
        "workflows_terminated": report.workflows_terminated,
        "workflows_unreachable": report.workflows_unreachable,
    }
