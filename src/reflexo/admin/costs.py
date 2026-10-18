"""The Costs page (BETA_12 §8.1, BETA_13 §3): what the AI costs, from `model_calls`, and each business's daily cap.

Read from `model_calls` rather than the `cost_daily` rollup, because the page groups by model and route, which the
rollup does not keep. Days are India days (the platform's), except each business's spend today, which is counted in
its own time zone as metering does. Amounts stay in micro-rupees; the web app formats them.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.costs import price_book
from reflexo.common.db import ServiceReason, get_db

GroupBy = Literal["day", "business", "feature", "model", "route"]
MAX_DAYS = 92
CAP_MIN_INR, CAP_MAX_INR = 1, 10_000
_TZ = "Asia/Kolkata"
_KEYS: dict[str, str] = {
    "day": f"((c.created_at AT TIME ZONE '{_TZ}')::date)::text",
    "business": "c.business_id::text",
    "feature": "c.feature",
    "model": "c.model",
    "route": "COALESCE(c.role, c.purpose)",
}
_WHERE = f"""c.created_at >= (CAST(:since AS date)::timestamp AT TIME ZONE '{_TZ}')
            AND c.created_at < ((CAST(:until AS date) + 1)::timestamp AT TIME ZONE '{_TZ}')
            AND (CAST(:b AS uuid[]) IS NULL OR c.business_id = ANY(CAST(:b AS uuid[])))"""


def window(since: date | None, until: date | None, today: date) -> tuple[date, date]:
    """The asked range, defaulting to the last 7 days; at most MAX_DAYS long."""
    until = until or today
    since = since or until - timedelta(days=6)
    if since > until:
        raise ValueError("from is after to")
    if (until - since).days + 1 > MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days")
    return since, until


async def overview(businesses: list[UUID] | None, since: date, until: date, group_by: GroupBy) -> dict[str, Any]:
    """`businesses`: None for every business (operators), else only these (a reviewer sees the showcase shops)."""
    args = {"since": since, "until": until, "b": businesses}
    order = "key" if group_by == "day" else "cost DESC"
    async with get_db().service_session(ServiceReason.COSTS) as s:
        rows = (
            await s.execute(
                text(f"""SELECT {_KEYS[group_by]} AS key, sum(c.cost_micro_inr) AS cost, count(*) AS calls,
                                sum(c.input_tokens) AS inp, sum(c.cached_input_tokens) AS cached
                         FROM model_calls c WHERE {_WHERE} GROUP BY 1 ORDER BY {order}"""),  # noqa: S608 - fixed map
                args,
            )
        ).all()
        turns = (
            await s.execute(
                text(f"""SELECT count(DISTINCT c.turn_id) FILTER (WHERE c.feature = 'chat') AS turns,
                                COALESCE(sum(c.cost_micro_inr) FILTER (WHERE c.feature = 'chat'), 0) AS turn_cost,
                                count(DISTINCT t.conversation_id) AS convs,
                                COALESCE(sum(c.cost_micro_inr), 0) AS conv_cost
                         FROM model_calls c JOIN conversation_turns t ON t.id = c.turn_id WHERE {_WHERE}"""),  # noqa: S608
                args,
            )
        ).one()
        fx = (await s.execute(text("SELECT day, usd_inr, source FROM fx_rates ORDER BY day DESC LIMIT 1"))).first()
        names = {
            str(r.id): r.name
            for r in (await s.execute(text("SELECT id, name FROM businesses WHERE id = ANY(:ids)"),
                                      {"ids": [UUID(str(r.key)) for r in rows] if group_by == "business" else []}))
        }  # fmt: skip
    cost = sum(int(r.cost) for r in rows)
    inp = sum(int(r.inp) for r in rows)
    return {
        "from": since.isoformat(),
        "to": until.isoformat(),
        "group_by": group_by,
        "totals": {
            "cost_micro_inr": cost,
            "calls": sum(int(r.calls) for r in rows),
            "chat_turns": int(turns.turns),
            "conversations": int(turns.convs),
            "cost_per_turn_micro_inr": round(int(turns.turn_cost) / int(turns.turns)) if turns.turns else None,
            "cost_per_conversation_micro_inr": round(int(turns.conv_cost) / int(turns.convs)) if turns.convs else None,
            "cached_share": round(sum(int(r.cached) for r in rows) / inp, 4) if inp else None,
        },
        "rows": [
            {
                "key": str(r.key),
                "label": names.get(str(r.key), str(r.key)),
                "cost_micro_inr": int(r.cost),
                "calls": int(r.calls),
                "input_tokens": int(r.inp),
                "cached_input_tokens": int(r.cached),
            }
            for r in rows
        ],
        "fx": {"usd_inr": float(fx.usd_inr), "day": fx.day.isoformat(), "source": fx.source} if fx else None,
        "price_version": price_book().version,
        "caps": await caps(businesses),
    }


async def caps(businesses: list[UUID] | None) -> list[dict[str, Any]]:
    """Daily cap and spend today: for the businesses asked, or for every business that has a cap or spent today."""
    default = int(Decimal(str(get_settings().cost_cap_daily_inr)) * 1_000_000)
    async with get_db().service_session(ServiceReason.COSTS) as s:
        rows = (
            await s.execute(
                text("""SELECT b.id, b.name, cc.daily_cap_micro_inr, cc.alert_at_pct, cc.alerted_on,
                               (SELECT COALESCE(sum(m.cost_micro_inr), 0) FROM model_calls m
                                 WHERE m.business_id = b.id
                                   AND m.created_at >= (date_trunc('day', now() AT TIME ZONE b.timezone)
                                                        AT TIME ZONE b.timezone)) AS today
                        FROM businesses b LEFT JOIN cost_caps cc ON cc.business_id = b.id
                        WHERE CAST(:b AS uuid[]) IS NULL OR b.id = ANY(CAST(:b AS uuid[]))"""),
                {"b": businesses},
            )
        ).all()
    shown = [r for r in rows if businesses is not None or r.daily_cap_micro_inr is not None or int(r.today) > 0]
    return sorted(
        (
            {
                "business_id": str(r.id),
                "name": r.name,
                "daily_cap_micro_inr": int(r.daily_cap_micro_inr) if r.daily_cap_micro_inr is not None else default,
                "default_cap": r.daily_cap_micro_inr is None,
                "alert_at_pct": int(r.alert_at_pct) if r.alert_at_pct is not None else 80,
                "alerted_on": r.alerted_on.isoformat() if r.alerted_on else None,
                "today_micro_inr": int(r.today),
            }
            for r in shown
        ),
        key=lambda c: -int(c["today_micro_inr"]),
    )


async def set_cap(business_id: UUID, daily_cap_inr: float) -> dict[str, Any]:
    """Raise or lower one business's daily cap. Metering reads the cap on every check, so it applies at once."""
    if not CAP_MIN_INR <= daily_cap_inr <= CAP_MAX_INR:
        raise ValueError(f"the cap must be between ₹{CAP_MIN_INR} and ₹{CAP_MAX_INR:,}")
    micro = int(Decimal(str(daily_cap_inr)) * 1_000_000)
    async with get_db().service_session(ServiceReason.COSTS) as s:
        await s.execute(
            text("""INSERT INTO cost_caps (business_id, daily_cap_micro_inr) VALUES (:b, :c)
                    ON CONFLICT (business_id) DO UPDATE SET daily_cap_micro_inr = :c, updated_at = now()"""),
            {"b": business_id, "c": micro},
        )
    return (await caps([business_id]))[0]
