"""Model-call metering (BETA_12 §7, §8.3; BETA_02 §4.7): one `model_calls` row per metered call, the daily cost
counter per business and the cost-cap alert.

* Every model call, embedding, transcription and voice-usage block writes one append-only row with its cost in
  micro-rupees, priced from the price file at the day's rupee rate (`common.costs`).
* The counter `cost:{business_id}:{yyyymmdd}` (business time) is incremented per row. If Redis is down the day's
  sum comes from `model_calls`; if the key was lost mid-day it is re-seeded from that sum.
* At `alert_at_pct` of the cap (default 80%) the crossing is recorded once per business day: `cost_caps.alerted_on`,
  an X-ray `cost_cap` event and an error log. The owner and operator messages are delivered by WP-26 (purpose
  `owner.cost_cap_warning`) and WP-05 (the operator on the Reflexo bot); decided with Taha on 18 Oct 2026.
* Metering never fails the call it measures: errors are logged and swallowed, like X-ray.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer
from reflexo.common.costs import Priced, Usage, price_call
from reflexo.common.db import get_db
from reflexo.common.enums import ModelCallKind, PipelineEventStatus
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx
from reflexo.common.logging import get_logger
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.common.timeutil import utcnow
from reflexo.common.tracing import current_trace_id
from reflexo.common.xray import emit

log = get_logger(__name__)
COUNTER_TTL_S = 2 * 86400
PRICED_AS = {"fake": "gemini"}  # the offline fake imitates Gemini, so its calls are priced (and tested) as Gemini's

# CallCtx.purpose -> model_calls.feature. A purpose not listed here is 'other' until it is given a feature.
FEATURE_BY_PURPOSE = {
    "customer_turn": "chat",
    "owner_turn": "chat",
    "media.describe": "chat",
    "answer_scope": "chat",
    "learn_rule": "chat",
    "graph": "chat",
    "automation.build": "automation_build",
    "import.columns": "import",
    "import.extract": "import",
    "onboarding_preview": "setup",
    "onboarding.preview_questions": "setup",
    "voice.summary": "voice",
    "voice.call": "voice",
    "embedding": "embedding",
}
CAP_WARNING_QUEUE = "costs.cap_warning"  # the owner is told at the alert share (BETA_12 §8.3)


def feature_of(ctx: CallCtx) -> str:
    return ctx.feature or FEATURE_BY_PURPOSE.get(ctx.purpose, "other")


@dataclass(frozen=True)
class Metered:
    """The row written for one call (returned for spans and tests)."""

    id: UUID
    business_id: UUID
    cost_micro_inr: int
    price_version: str
    fx_rate: Decimal


async def record(
    ctx: CallCtx | None,
    *,
    kind: ModelCallKind,
    provider: str,
    model: str,
    usage: Usage,
    duration_ms: int,
    role: str | None = None,
    ttft_ms: int | None = None,
    hedged: bool = False,
    status: str = "ok",
    error_code: str | None = None,
    feature: str | None = None,
) -> Metered | None:
    """Write the `model_calls` row for one call. Returns None (and logs) if it could not be written."""
    if ctx is None or ctx.business_id is None:
        log.warning("model_call_unattributed", provider=provider, model=model, purpose=ctx.purpose if ctx else None)
        return None
    try:
        priced = await price_call(PRICED_AS.get(provider, provider), model, usage)
        row_id = new_id()
        fields = {
            "kind": str(kind),
            "provider": provider,
            "model": model,
            "role": role,
            "feature": feature or feature_of(ctx),
            "ms": max(int(duration_ms), 0),
            "ttft": ttft_ms,
            "hedged": hedged,
            "status": status,
            "err": error_code,
        }
        tz = await _insert(row_id, ctx, priced, usage, fields)
        await _count(ctx.business_id, priced.cost_micro_inr, tz)
    except Exception as exc:  # metering must never break the call it measures
        log.error("model_call_record_failed", provider=provider, model=model, error=f"{type(exc).__name__}: {exc}")
        return None
    return Metered(row_id, ctx.business_id, priced.cost_micro_inr, priced.price_version, priced.fx_rate)


async def _insert(row_id: UUID, ctx: CallCtx, priced: Priced, usage: Usage, fields: dict[str, Any]) -> str:
    assert ctx.business_id is not None
    async with get_db().tenant_session(ctx.business_id) as s:
        tz = (
            await s.execute(
                text("""INSERT INTO model_calls (id, business_id, turn_id, run_ref, feature, kind, purpose, role,
                          provider, model, prompt_version, input_tokens, cached_input_tokens, output_tokens,
                          audio_seconds, characters, cost_micro_inr, price_version, fx_rate, duration_ms, ttft_ms,
                          hedged, status, error_code, trace_id)
                        VALUES (:id, :b, :turn, :run, :feature, CAST(:kind AS model_call_kind), :purpose, :role,
                          :provider, :model, :pv, :inp, :cached, :out, :audio, :chars, :cost, :price_v, :fx, :ms,
                          :ttft, :hedged, :status, :err, :trace)
                        RETURNING (SELECT timezone FROM businesses WHERE id = :b)"""),
                {
                    **fields,
                    "id": row_id,
                    "b": ctx.business_id,
                    "turn": ctx.turn_id,
                    "run": ctx.run_ref,
                    "purpose": ctx.purpose,
                    "pv": ctx.prompt_version,
                    "inp": usage.input_tokens,
                    "cached": usage.cached_input_tokens,
                    "out": usage.output_tokens,
                    "audio": usage.audio_seconds,
                    "chars": usage.characters,
                    "cost": priced.cost_micro_inr,
                    "price_v": priced.price_version,
                    "fx": priced.fx_rate,
                    "trace": current_trace_id(),
                },
            )
        ).scalar_one()
    return str(tz or "Asia/Kolkata")


# ---------------------------------------------------------------- the daily counter and the cap


def business_day(tz: str) -> date:
    return utcnow().astimezone(ZoneInfo(tz)).date()


def counter_key(business_id: UUID, day: date) -> str:
    return f"cost:{business_id}:{day:%Y%m%d}"


async def spent_today(business_id: UUID, tz: str | None = None) -> int:
    """Today's cost in micro-rupees (business time): the Redis counter, else the `model_calls` sum."""
    tz = tz or await _timezone(business_id)
    day = business_day(tz)
    try:
        value = await get_redis().call("get", counter_key(business_id, day))
        if value is not None:
            return int(value)
    except RedisUnavailable:
        pass
    return await _sum_for_day(business_id, day, tz)


async def _count(business_id: UUID, cost: int, tz: str) -> None:
    day = business_day(tz)
    key = counter_key(business_id, day)
    try:
        total = int(await get_redis().call("incrby", key, cost))
        if total == cost:  # a fresh key: the day may have started before it (lost or evicted), so seed it
            total = await _sum_for_day(business_id, day, tz)
            await get_redis().call("set", key, total, ex=COUNTER_TTL_S)
        else:
            await get_redis().call("expire", key, COUNTER_TTL_S)
    except RedisUnavailable:
        total = await _sum_for_day(business_id, day, tz)
    await _check_cap(business_id, total, day)


async def _sum_for_day(business_id: UUID, day: date, tz: str) -> int:
    async with get_db().tenant_session(business_id) as s:
        return int(
            (
                await s.execute(
                    text("""SELECT COALESCE(sum(cost_micro_inr), 0) FROM model_calls
                            WHERE business_id = :b AND (created_at AT TIME ZONE :tz)::date = :d"""),
                    {"b": business_id, "tz": tz, "d": day},
                )
            ).scalar_one()
        )


async def _timezone(business_id: UUID) -> str:
    async with get_db().tenant_session(business_id) as s:
        tz = (
            await s.execute(text("SELECT timezone FROM businesses WHERE id = :b"), {"b": business_id})
        ).scalar_one_or_none()
    return str(tz or "Asia/Kolkata")


def default_cap_micro_inr() -> int:
    return int(Decimal(str(get_settings().cost_cap_daily_inr)) * 1_000_000)


async def _check_cap(business_id: UUID, total: int, day: date) -> None:
    """Record the alert crossing once per business day (the first call past the threshold wins the update)."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("SELECT daily_cap_micro_inr, alert_at_pct, alerted_on FROM cost_caps WHERE business_id = :b"),
                {"b": business_id},
            )
        ).first()
        cap = int(row.daily_cap_micro_inr) if row else default_cap_micro_inr()
        alert_pct = int(row.alert_at_pct) if row else 80
        if total * 100 < cap * alert_pct or (row is not None and row.alerted_on == day):
            return
        fired = (
            await s.execute(
                text("""INSERT INTO cost_caps (business_id, daily_cap_micro_inr, alerted_on)
                        VALUES (:b, :cap, :d)
                        ON CONFLICT (business_id) DO UPDATE SET alerted_on = :d, updated_at = now()
                        WHERE cost_caps.alerted_on IS DISTINCT FROM :d
                        RETURNING business_id"""),
                {"b": business_id, "cap": cap, "d": day},
            )
        ).first()
    if fired is None:
        return
    pct = round(total * 100 / cap, 1) if cap else 0.0
    log.error("cost_cap_alert", business_id=str(business_id), spent_micro_inr=total, cap_micro_inr=cap, pct=pct)
    await emit(
        "cost_cap",
        PipelineEventStatus.ERROR,
        business_id=business_id,
        level="alert",
        spent_micro_inr=total,
        cap_micro_inr=cap,
        pct=pct,
    )
    enqueuer = ports.registry.optional(JobEnqueuer)  # type: ignore[type-abstract]
    if enqueuer is not None:  # the owner is told by a job: a turn never waits on writing the warning
        await enqueuer.enqueue(
            CAP_WARNING_QUEUE,
            {"business_id": str(business_id), "spent_micro_inr": total, "cap_micro_inr": cap, "pct": pct},
            business_id=business_id,
            dedupe_key=f"costcap:{business_id}:{day.isoformat()}",
        )


async def turn_cost(business_id: UUID, turn_id: UUID) -> dict[str, int]:
    """A turn's metered totals from `model_calls` (what X-ray shows; CHK-PC-3)."""
    async with get_db().tenant_session(business_id) as s:
        r = (
            await s.execute(
                text("""SELECT count(*) AS calls, COALESCE(sum(cost_micro_inr), 0) AS cost,
                               COALESCE(sum(input_tokens), 0) AS inp, COALESCE(sum(cached_input_tokens), 0) AS cached,
                               COALESCE(sum(output_tokens), 0) AS out
                        FROM model_calls WHERE turn_id = :t"""),
                {"t": turn_id},
            )
        ).one()
    return {
        "model_calls": int(r.calls),
        "cost_micro_inr": int(r.cost),
        "input_tokens": int(r.inp),
        "cached_input_tokens": int(r.cached),
        "output_tokens": int(r.out),
    }
