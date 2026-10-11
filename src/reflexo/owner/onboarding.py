"""Onboarding state machine (POC_05 §3.4): sources_requested → extracting → gap_questions → preview → live.

Gap questions cover only essentials the business has not given yet (max 5, skippable). An answer is saved as an
owner-sourced Q&A through an auto-applied change set, so customers get it immediately and it is undoable.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.knowledge.api as knowledge
from reflexo.common.db import get_db
from reflexo.common.ids import new_id

STATES = ("sources_requested", "extracting", "gap_questions", "preview", "live")
WELCOME = "Namaste! Apna menu ya price list bhejiye — photo, PDF ya Excel — ya type kar dijiye. 📄"
LIVE = "Ho gaya! 🎉 Ab customers ko main handle karungi. Kuch badalna ho to bas mujhe bata dijiye."


@dataclass(frozen=True)
class Gap:
    key: str
    ask: str  # to the owner
    customer_question: str  # how customers ask it (the saved Q&A's question)
    missing: Callable[[UUID], Awaitable[bool]]


async def _no_delivery(business_id: UUID) -> bool:
    profile = await knowledge.get_profile(business_id)
    return not profile.delivery.zones and not profile.delivery.pickup_available


async def _no_payments(business_id: UUID) -> bool:
    profile = await knowledge.get_profile(business_id)
    settings = await knowledge.business_settings(business_id)
    return not profile.payments.methods and not settings.get("cod_rule")


async def _no_hours(business_id: UUID) -> bool:
    return not (await knowledge.get_profile(business_id)).hours.weekly


async def _no_eggless(business_id: UUID) -> bool:
    hits = await knowledge.search_knowledge(business_id, "eggless", k=1)
    if hits:
        return False
    cards = await knowledge.search_catalog(business_id, "cake", k=10)
    return not any(c.attributes.get("eggless_available") is not None for c in cards)


GAPS = [
    Gap(
        "delivery",
        "Delivery kahan tak karte hain aur charge kitna hai? (ya sirf pickup?)",
        "Do you deliver? What are the delivery charges?",
        _no_delivery,
    ),
    Gap("payments", "Payment kaise lete hain — UPI, card, cash on delivery?", "How can I pay?", _no_payments),
    Gap("eggless", "Eggless cakes milte hain? Extra charge?", "Do you have eggless cakes?", _no_eggless),
    Gap("hours", "Shop ki timing kya hai?", "What are your timings?", _no_hours),
]


async def get(business_id: UUID) -> dict[str, Any] | None:
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("SELECT state, data FROM onboarding_sessions WHERE business_id = :b"), {"b": business_id}
            )
        ).first()
    return {"state": row.state, "data": dict(row.data or {})} if row else None


async def set_state(business_id: UUID, state: str, data: dict[str, Any] | None = None) -> None:
    if state not in STATES:
        raise ValueError(state)
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO onboarding_sessions (id, business_id, state, data)
                    VALUES (:id, :b, :st, CAST(:d AS jsonb))
                    ON CONFLICT (business_id) DO UPDATE SET state = EXCLUDED.state,
                        data = onboarding_sessions.data || EXCLUDED.data,
                        completed_at = CASE WHEN EXCLUDED.state = 'live' THEN now() ELSE NULL END"""),
            {"id": new_id(), "b": business_id, "st": state, "d": json.dumps(data or {})},
        )


async def start(business_id: UUID) -> None:
    """A business enters onboarding (demo reset): AI is not live for customers until `go_live`."""
    async with get_db().tenant_session(business_id) as s:
        await s.execute(text("UPDATE businesses SET status = 'onboarding' WHERE id = :b"), {"b": business_id})
        await s.execute(text("DELETE FROM onboarding_sessions WHERE business_id = :b"), {"b": business_id})
    await set_state(business_id, "sources_requested", {"asked": [], "skipped": []})


async def next_gap(business_id: UUID) -> Gap | None:
    session = await get(business_id)
    done = set((session or {}).get("data", {}).get("asked", [])) | set(
        (session or {}).get("data", {}).get("skipped", [])
    )
    for gap in GAPS:
        if gap.key not in done and await gap.missing(business_id):
            return gap
    return None


async def record_gap(business_id: UUID, key: str, answer: str | None, member_id: UUID | None) -> None:
    """Save the owner's answer as a Q&A for customers (or mark the question skipped)."""
    gap = next(g for g in GAPS if g.key == key)
    session = await get(business_id) or {"data": {}}
    data = session["data"]
    if answer and answer.strip():
        await knowledge.apply_now(
            business_id,
            "onboarding",
            f"Onboarding: {key}",
            [("qa", "create", None, {"question": gap.customer_question, "answer": answer.strip(), "scope": "global"})],
            member_id=member_id,
        )
        data["asked"] = sorted({*data.get("asked", []), key})
    else:
        data["skipped"] = sorted({*data.get("skipped", []), key})
    await set_state(business_id, "gap_questions", data)


async def go_live(business_id: UUID) -> None:
    await set_state(business_id, "live")
    async with get_db().tenant_session(business_id) as s:
        await s.execute(text("UPDATE businesses SET status = 'active' WHERE id = :b"), {"b": business_id})
