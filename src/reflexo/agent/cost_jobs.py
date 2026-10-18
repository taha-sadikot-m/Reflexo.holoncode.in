"""Daily cost jobs (BETA_12 §6.2, §8; BETA_02 §4.7).

* `costs.fx_refresh` (06:00 IST): USD->INR from Frankfurter (ECB reference rates), else open.er-api.com. If both
  fail, yesterday's rate is carried forward as today's row (`source = 'carried_forward'`), so every day has a rate
  and the run of failures is visible. After `REFLEXO_FX_ALERT_AFTER_DAYS` carried days in a row the operator is
  alerted (an X-ray `alert` event and an error log; delivery on the Reflexo bot comes with WP-05).
* `costs.rollup` (00:30 IST): rebuilds `cost_daily` for yesterday and today, per business day and feature, from
  `model_calls`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.costs import forget_fx_cache
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import IST, utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)
FX_QUEUE = "costs.fx_refresh"
ROLLUP_QUEUE = "costs.rollup"
ROLLUP_AT = "00:30"
FX_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _rate_from(source: str, body: dict[str, Any]) -> Decimal:
    """Both sources answer `{"rates": {"INR": <number>}, ...}`; open.er-api also says `"result": "success"`."""
    if source == "open_er_api" and body.get("result") not in (None, "success"):
        raise ValueError(f"open.er-api result {body.get('result')}")
    rate = Decimal(str((body.get("rates") or {})["INR"]))
    if not Decimal(10) < rate < Decimal(1000):  # a sanity bound: a broken answer must not price every call
        raise ValueError(f"implausible USD->INR rate {rate}")
    return rate


async def _fetch(client: httpx.AsyncClient) -> tuple[Decimal, str] | None:
    s = get_settings()
    for source, url in (("frankfurter", s.fx_primary_url), ("open_er_api", s.fx_fallback_url)):
        try:
            resp = await client.get(url, timeout=FX_TIMEOUT)
            resp.raise_for_status()
            return _rate_from(source, resp.json()), source
        except Exception as exc:  # each source may fail on its own; the next one is tried
            log.warning("fx_source_failed", source=source, error=f"{type(exc).__name__}: {str(exc)[:200]}")
    return None


async def fx_refresh(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Write today's rate (IST date). Returns what was written, for the job log and tests."""
    today = utcnow().astimezone(IST).date()
    if client is None:
        async with httpx.AsyncClient() as own:
            fetched = await _fetch(own)
    else:
        fetched = await _fetch(client)
    async with get_db().service_session(ServiceReason.COSTS) as s:
        if fetched is None:
            last = (
                await s.execute(
                    text("SELECT usd_inr FROM fx_rates WHERE day < :d ORDER BY day DESC LIMIT 1"), {"d": today}
                )
            ).scalar_one_or_none()
            if last is None:
                raise RuntimeError("no fx rate can be fetched and none is known")
            rate, source = Decimal(str(last)), "carried_forward"
        else:
            rate, source = fetched
        await s.execute(
            text("""INSERT INTO fx_rates (day, usd_inr, source) VALUES (:d, :r, :src)
                    ON CONFLICT (day) DO UPDATE SET usd_inr = EXCLUDED.usd_inr, source = EXCLUDED.source,
                                                    fetched_at = now()"""),
            {"d": today, "r": rate, "src": source},
        )
        carried = await _carried_days(s, today)
    forget_fx_cache()
    if carried >= get_settings().fx_alert_after_days:
        log.error("fx_refresh_failing", days=carried, rate=str(rate))
        await emit("alert", PipelineEventStatus.ERROR, kind="fx_refresh_failing", days=carried, usd_inr=str(rate))
    shown = str(rate.quantize(Decimal("0.0001")))
    return {"day": today.isoformat(), "usd_inr": shown, "source": source, "carried_days": carried}


async def _carried_days(s: Any, today: date) -> int:
    """How many days in a row, ending today, the rate was carried forward."""
    rows = (
        await s.execute(
            text("SELECT day, source FROM fx_rates WHERE day <= :d ORDER BY day DESC LIMIT 31"), {"d": today}
        )
    ).all()
    count, expected = 0, today
    for r in rows:
        if r.day != expected or r.source != "carried_forward":
            break
        count += 1
        expected = expected - timedelta(days=1)
    return count


async def rollup(days: int = 2) -> int:
    """Rebuild `cost_daily` for the last `days` business days (each business in its own timezone)."""
    since = utcnow() - timedelta(days=days + 1)
    async with get_db().service_session(ServiceReason.COSTS) as s:
        await s.execute(
            text("""DELETE FROM cost_daily cd USING businesses b
                    WHERE b.id = cd.business_id
                      AND cd.day >= ((now() AT TIME ZONE b.timezone)::date - CAST(:days AS int) + 1)"""),
            {"days": days},
        )
        written = await s.execute(
            text("""INSERT INTO cost_daily (business_id, day, feature, calls, input_tokens, cached_input_tokens,
                                            output_tokens, audio_seconds, cost_micro_inr)
                    SELECT m.business_id, (m.created_at AT TIME ZONE b.timezone)::date AS day, m.feature, count(*),
                           sum(m.input_tokens), sum(m.cached_input_tokens), sum(m.output_tokens),
                           sum(m.audio_seconds), sum(m.cost_micro_inr)
                    FROM model_calls m JOIN businesses b ON b.id = m.business_id
                    WHERE m.created_at >= :since
                      AND (m.created_at AT TIME ZONE b.timezone)::date
                          >= ((now() AT TIME ZONE b.timezone)::date - CAST(:days AS int) + 1)
                    GROUP BY m.business_id, day, m.feature
                    RETURNING 1"""),
            {"since": since, "days": days},
        )
        return len(written.all())
