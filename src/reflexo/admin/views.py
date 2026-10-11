"""Admin read models (POC_07 §2, §4, §6): businesses, health, X-ray traces, conversations.

Cross-tenant queries (health, business list) use the service role; everything shown for one business is read
under that business's RLS context, so the switcher can never leak another tenant's rows (CHK-F12-2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.logging import mask_phone_numbers
from reflexo.common.redis import get_redis

HEARTBEAT_FRESH_S = 15


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- businesses & health


async def businesses() -> list[dict[str, Any]]:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (
            await s.execute(text("SELECT id, name, slug, status, kill_switch FROM businesses ORDER BY created_at"))
        ).all()
    return [
        {"id": str(r.id), "name": r.name, "slug": r.slug, "status": str(r.status), "kill_switch": r.kill_switch}
        for r in rows
    ]


async def health() -> dict[str, Any]:
    out: dict[str, Any] = {"postgres": "ok", "redis": "ok" if await get_redis().ping() else "degraded"}
    try:
        async with get_db().service_session(ServiceReason.HEALTH) as s:
            workers = (
                await s.execute(
                    text("""SELECT worker_id, service, pools, started_at, beat_at, stats,
                                   extract(epoch FROM now() - beat_at) AS age_s
                            FROM worker_heartbeats ORDER BY beat_at DESC LIMIT 20""")
                )
            ).all()
            queues = (
                await s.execute(
                    text("""
                SELECT 'inbound' AS pool, count(*) AS depth, extract(epoch FROM now() - min(received_at)) AS oldest_s
                  FROM inbox_events WHERE status = 'pending'
                UNION ALL
                SELECT 'turns', count(*), extract(epoch FROM now() - min(run_after))
                  FROM conversation_turns WHERE status = 'pending' AND run_after <= now()
                UNION ALL
                SELECT 'outbound', count(*), extract(epoch FROM now() - min(next_attempt_at))
                  FROM outbound_messages WHERE status IN ('queued', 'unconfirmed') AND next_attempt_at <= now()
                UNION ALL
                SELECT 'jobs', count(*), extract(epoch FROM now() - min(run_after))
                  FROM jobs WHERE status = 'pending' AND run_after <= now()""")
                )
            ).all()
            running = (
                await s.execute(text("SELECT count(*) FROM conversation_turns WHERE status = 'running'"))
            ).scalar_one()
            dead = (await s.execute(text("SELECT count(*) FROM jobs WHERE status = 'dead'"))).scalar_one()
    except Exception as exc:  # health must report, not raise
        return {"postgres": f"error: {type(exc).__name__}", "redis": out["redis"], "workers": [], "queues": []}
    out["workers"] = [
        {
            "id": w.worker_id,
            "service": w.service,
            "pools": list(w.pools),
            "started_at": _iso(w.started_at),
            "age_s": round(float(w.age_s), 1),
            "alive": float(w.age_s) < HEARTBEAT_FRESH_S,
            "stats": w.stats,
        }
        for w in workers
    ]
    out["queues"] = [
        {"pool": q.pool, "depth": int(q.depth), "oldest_s": round(float(q.oldest_s), 1) if q.oldest_s else 0.0}
        for q in queues
    ]
    out["turns_running"] = int(running)
    out["dead_letters"] = int(dead)
    return out


# ---------------------------------------------------------------- X-ray


# a failed gate check is a normal repair round, and a failed model attempt may be hedged/failed over; these are
# failures of the turn or the delivery:
_FAILURE_STAGES = ("turn_error", "ingress", "inbox", "parse")


def _is_failure(e: Any) -> bool:
    return str(e.status) == "error" and (e.stage in _FAILURE_STAGES or e.stage.startswith("outbound_"))


def _summarize(trace_id: str, events: list[Any]) -> dict[str, Any]:
    first, last = events[0], events[-1]
    start = min((e.created_at.timestamp() * 1000 - (e.duration_ms or 0)) for e in events)
    end = max(e.created_at.timestamp() * 1000 for e in events)
    attrs = [e.attrs or {} for e in events]
    stages = [e.stage for e in events]
    model_calls = [a for e, a in zip(events, attrs, strict=True) if e.stage == "model_call" and a.get("model")]
    reply = next((a for e, a in zip(events, attrs, strict=True) if e.stage == "reply"), None)
    gate = next((e for e in events if e.stage == "gate"), None)
    provider = next((a.get("provider") for a in attrs if a.get("provider")), None)
    outcome = None
    if reply:
        outcome = reply.get("action")
    elif "automation_route" in stages and any(a.get("result") == "consumed" for a in attrs):
        outcome = "consumed_by_automation"
    elif "call_route" in stages and any(a.get("result") in ("delivered", "buffered") for a in attrs):
        outcome = "routed_to_call"
    elif "turn_superseded" in stages:
        outcome = "superseded"
    elif any(_is_failure(e) for e in events):
        outcome = "error"
    return {
        "trace_id": trace_id,
        "started_at": _iso(first.created_at),
        "total_ms": int(end - start),
        "stages": len(events),
        "provider": provider,
        "conversation_id": next((str(e.conversation_id) for e in events if e.conversation_id), None),
        "turn_id": next((str(e.turn_id) for e in events if e.turn_id), None),
        "outcome": outcome,
        "model": model_calls[-1].get("model") if model_calls else None,
        "hedged": any(a.get("hedged") for a in model_calls),
        "gate": None if gate is None else str(gate.status) == "ok",
        "simulated": any(a.get("simulated") for a in attrs),
        "errors": sum(1 for e in events if _is_failure(e)),
        "gate_repairs": sum(1 for e in events if e.stage == "gate" and str(e.status) == "error"),
        "last_stage": last.stage,
    }


async def traces(business_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        ids = [
            r.trace_id
            for r in (
                await s.execute(
                    text("""SELECT trace_id, max(created_at) AS last_at FROM pipeline_events
                            WHERE business_id = :b AND trace_id <> 'untraced'
                            GROUP BY trace_id ORDER BY last_at DESC LIMIT :n"""),
                    {"b": business_id, "n": limit},
                )
            ).all()
        ]
        if not ids:
            return []
        rows = (
            await s.execute(
                text("""SELECT * FROM pipeline_events WHERE trace_id = ANY(:ids)
                          AND (business_id = :b OR business_id IS NULL) ORDER BY created_at, id"""),
                {"ids": ids, "b": business_id},
            )
        ).all()
        turn_ids = list({r.turn_id for r in rows if r.turn_id})
        snippets = {
            r.turn_id: r.content_text
            for r in (
                await s.execute(
                    text("""SELECT DISTINCT ON (turn_id) turn_id, content_text FROM messages
                            WHERE business_id = :b AND turn_id = ANY(:t) AND direction = 'inbound'
                            ORDER BY turn_id, created_at"""),
                    {"b": business_id, "t": turn_ids},
                )
            ).all()
        }
    grouped: dict[str, list[Any]] = {}
    for r in rows:
        grouped.setdefault(r.trace_id, []).append(r)
    out = []
    for tid in ids:
        summary = _summarize(tid, grouped[tid])
        turn = summary["turn_id"]
        text_ = snippets.get(UUID(turn)) if turn else None
        summary["snippet"] = mask_phone_numbers(text_[:120]) if text_ else None
        out.append(summary)
    return out


async def trace_detail(business_id: UUID, trace_id: str) -> dict[str, Any] | None:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (
            await s.execute(
                text("""SELECT * FROM pipeline_events WHERE trace_id = :t
                          AND (business_id = :b OR business_id IS NULL) ORDER BY created_at, id"""),
                {"t": trace_id, "b": business_id},
            )
        ).all()
        if not rows or not any(r.business_id == business_id for r in rows):
            return None  # not this business's trace
        turn_ids = list({r.turn_id for r in rows if r.turn_id})
        runs = (
            await s.execute(
                text("""SELECT turn_id, model_primary, model_used, hedged, model_calls, input_tokens, output_tokens,
                               latency_ms, latency_breakdown, topic, tier, grounded, gate_result, outcome, error,
                               prompt_version
                        FROM agent_runs WHERE business_id = :b AND turn_id = ANY(:t) ORDER BY created_at"""),
                {"b": business_id, "t": turn_ids},
            )
        ).all()
        parts = (
            await s.execute(
                text("""SELECT o.id, o.part_index, o.status, o.provider_message_id, o.idempotency_key, o.attempts,
                               o.sent_at, m.turn_id
                        FROM outbound_messages o JOIN messages m ON m.id = o.message_id
                        WHERE o.business_id = :b AND m.turn_id = ANY(:t) ORDER BY o.created_at, o.part_index"""),
                {"b": business_id, "t": turn_ids},
            )
        ).all()
    t0 = min(r.created_at.timestamp() * 1000 - (r.duration_ms or 0) for r in rows)
    events = [
        {
            "id": str(r.id),
            "stage": r.stage,
            "status": str(r.status),
            "start_ms": int(r.created_at.timestamp() * 1000 - (r.duration_ms or 0) - t0),
            "duration_ms": r.duration_ms,
            "at": _iso(r.created_at),
            "turn_id": str(r.turn_id) if r.turn_id else None,
            "attrs": r.attrs or {},
        }
        for r in rows
    ]
    return {
        "summary": _summarize(trace_id, list(rows)),
        "events": events,
        "runs": [{k: (str(v) if isinstance(v, UUID) else v) for k, v in r._mapping.items()} for r in runs],
        "outbound": [
            {
                "id": str(p.id),
                "part": p.part_index,
                "status": str(p.status),
                "provider_message_id": p.provider_message_id,
                "idempotency_key": p.idempotency_key,
                "attempts": p.attempts,
                "sent_at": _iso(p.sent_at),
            }
            for p in parts
        ],
    }


async def events_since(business_id: UUID, after: datetime | None, limit: int = 200) -> list[dict[str, Any]]:
    """Backfill / polling source for the SSE stream (newest `limit`, returned oldest first)."""
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (
            await s.execute(
                text("""SELECT * FROM (
                          SELECT * FROM pipeline_events
                          WHERE (business_id = :b OR (business_id IS NULL AND trace_id IN (
                                   SELECT trace_id FROM pipeline_events WHERE business_id = :b
                                     AND created_at > now() - interval '1 hour')))
                            AND (CAST(:after AS timestamptz) IS NULL OR created_at > :after)
                          ORDER BY created_at DESC LIMIT :n) x ORDER BY created_at, id"""),
                {"b": business_id, "after": after, "n": limit},
            )
        ).all()
    return [event_json(r) for r in rows]


def event_json(r: Any) -> dict[str, Any]:
    return {
        "type": "xray",
        "id": str(r.id),
        "trace_id": r.trace_id,
        "stage": r.stage,
        "status": str(r.status),
        "duration_ms": r.duration_ms,
        "conversation_id": str(r.conversation_id) if r.conversation_id else None,
        "turn_id": str(r.turn_id) if r.turn_id else None,
        "attrs": r.attrs or {},
        "at": _iso(r.created_at),
    }


# ---------------------------------------------------------------- conversations


async def conversations(business_id: UUID, limit: int = 50, before: datetime | None = None) -> dict[str, Any]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT c.id, c.channel, c.participant_role, c.needs_attention, c.simulated,
                   c.human_active_until > now() AS human_active, cu.display_name AS customer_name,
                   cu.phone_e164, lm.content_text AS last_text, lm.direction AS last_direction,
                   lm.created_at AS last_at
            FROM conversations c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            JOIN LATERAL (SELECT content_text, direction, created_at FROM messages m
                          WHERE m.conversation_id = c.id ORDER BY created_at DESC LIMIT 1) lm ON true
            WHERE (CAST(:before AS timestamptz) IS NULL OR lm.created_at < :before)
            ORDER BY lm.created_at DESC LIMIT :n"""),
                {"before": before, "n": limit + 1},
            )
        ).all()
    items = [
        {
            "id": str(r.id),
            "channel": str(r.channel),
            "role": str(r.participant_role),
            "customer": r.customer_name or (mask_phone_numbers(r.phone_e164) if r.phone_e164 else None),
            "last_text": mask_phone_numbers((r.last_text or "")[:140]),
            "last_direction": str(r.last_direction),
            "last_at": _iso(r.last_at),
            "needs_attention": r.needs_attention,
            "human_active": bool(r.human_active),
            "simulated": r.simulated,
        }
        for r in rows[:limit]
    ]
    return {"items": items, "next_cursor": items[-1]["last_at"] if len(rows) > limit else None}


async def conversation_thread(business_id: UUID, conversation_id: UUID, limit: int = 200) -> dict[str, Any] | None:
    async with get_db().tenant_session(business_id) as s:
        conv = (
            await s.execute(
                text("""SELECT c.id, c.channel, c.participant_role, c.needs_attention, c.simulated, c.thread_key,
                               c.human_active_until, cu.id AS customer_id, cu.display_name, cu.phone_e164,
                               cu.language, cu.memory
                        FROM conversations c LEFT JOIN customers cu ON cu.id = c.customer_id WHERE c.id = :id"""),
                {"id": conversation_id},
            )
        ).first()
        if conv is None:
            return None
        msgs = (
            await s.execute(
                text("""SELECT * FROM (SELECT id, direction, author, content_text, content_blocks, attachments,
                                              interactive_reply, status, turn_id, error, created_at, delivered_at,
                                              read_at
                                       FROM messages WHERE conversation_id = :c ORDER BY created_at DESC LIMIT :n) x
                        ORDER BY created_at"""),
                {"c": conversation_id, "n": limit},
            )
        ).all()
    return {
        "id": str(conv.id),
        "channel": str(conv.channel),
        "role": str(conv.participant_role),
        "needs_attention": conv.needs_attention,
        "simulated": conv.simulated,
        "human_active_until": _iso(conv.human_active_until),
        "customer": None
        if conv.customer_id is None
        else {
            "id": str(conv.customer_id),
            "name": conv.display_name,
            "phone": mask_phone_numbers(conv.phone_e164) if conv.phone_e164 else None,
            "language": conv.language,
            "facts": [f for f in (conv.memory or {}).get("facts", [])],
        },
        "messages": [
            {
                "id": str(m.id),
                "direction": str(m.direction),
                "author": str(m.author),
                "text": m.content_text,
                "blocks": m.content_blocks,
                "attachments": m.attachments,
                "interactive": m.interactive_reply,
                "status": str(m.status),
                "turn_id": str(m.turn_id) if m.turn_id else None,
                "error": m.error,
                "at": _iso(m.created_at),
                "delivered_at": _iso(m.delivered_at),
                "read_at": _iso(m.read_at),
            }
            for m in msgs
        ],
    }
