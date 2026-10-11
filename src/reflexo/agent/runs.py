"""`agent_runs` rows: one per desk invocation, built from the run's accumulated `RunStats` (POC_04 §12)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx


async def record_run(
    s: AsyncSession,
    ctx: CallCtx,
    *,
    desk: str,
    prompt_version: str,
    model_primary: str,
    latency_ms: int | None = None,
    latency_breakdown: dict[str, Any] | None = None,
    outcome: str | None = None,
    topic: str | None = None,
    tier: str | None = None,
    grounded: bool | None = None,
    gate_result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    call_session_id: UUID | None = None,
) -> UUID:
    if ctx.business_id is None:
        raise ValueError("agent_runs needs a business_id")
    run_id = new_id()
    st = ctx.stats
    breakdown = dict(latency_breakdown or {})
    breakdown.setdefault("model_calls", st.calls)
    await s.execute(
        text("""
        INSERT INTO agent_runs (id, business_id, turn_id, call_session_id, desk, prompt_version, model_primary,
                                model_used, hedged, model_calls, input_tokens, output_tokens, latency_ms,
                                latency_breakdown, topic, tier, grounded, gate_result, outcome, error)
        VALUES (:id, :b, :turn, :call, CAST(:desk AS desk), :pv, :mp, :mu, :hedged, :calls, :inp, :outp, :lat,
                CAST(:lb AS jsonb), CAST(:topic AS topic), CAST(:tier AS risk_tier), :grounded,
                CAST(:gate AS jsonb), CAST(:outcome AS turn_outcome), CAST(:err AS jsonb))"""),
        {
            "id": run_id,
            "b": ctx.business_id,
            "turn": ctx.turn_id,
            "call": call_session_id,
            "desk": desk,
            "pv": prompt_version,
            "mp": model_primary,
            "mu": st.model_used,
            "hedged": st.hedged,
            "calls": st.model_calls,
            "inp": st.input_tokens,
            "outp": st.output_tokens,
            "lat": latency_ms,
            "lb": json.dumps(breakdown, default=str),
            "topic": topic,
            "tier": tier,
            "grounded": grounded,
            "gate": json.dumps(gate_result) if gate_result is not None else None,
            "outcome": outcome,
            "err": json.dumps(error) if error is not None else None,
        },
    )
    return run_id
