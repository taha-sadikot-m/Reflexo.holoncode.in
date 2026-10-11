"""Inbox-first ingestion (POC_03 §2): persist before acknowledging; duplicates hit UNIQUE(provider, dedupe_key)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.contracts import InboxEvent
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.tracing import current_trace_context
from reflexo.pipeline.jobs import backoff_s

MAX_INBOX_ATTEMPTS = 5


@dataclass(frozen=True)
class StoreResult:
    inserted: bool
    inbox_id: UUID | None


async def store(
    provider: str,
    dedupe_key: str,
    *,
    route_key: str | None,
    headers: dict[str, Any],
    payload: dict[str, Any],
    simulated: bool = False,
) -> StoreResult:
    """Insert a verified webhook. Raises on DB failure, so the route returns 503 and the provider retries."""
    async with get_db().service_session(ServiceReason.WEBHOOK_INGRESS) as s:
        inbox_id = (
            await s.execute(
                text("""
            INSERT INTO inbox_events (id, provider, route_key, dedupe_key, headers, payload, simulated, trace_context)
            VALUES (:id, CAST(:p AS channel_provider), :r, :k, CAST(:h AS jsonb), CAST(:body AS jsonb), :sim,
                    CAST(:trace AS jsonb))
            ON CONFLICT (provider, dedupe_key) DO NOTHING
            RETURNING id"""),
                {
                    "id": new_id(),
                    "p": provider,
                    "r": route_key,
                    "k": dedupe_key,
                    "h": json.dumps(headers),
                    "body": json.dumps(payload, ensure_ascii=False),
                    "sim": simulated,
                    "trace": json.dumps(current_trace_context()),
                },
            )
        ).scalar_one_or_none()
        if inbox_id is not None:
            await s.execute(text("SELECT pg_notify('q_inbound', '')"))
    return StoreResult(inserted=inbox_id is not None, inbox_id=inbox_id)


async def claim(worker: str, lease_s: int, limit: int) -> list[InboxEvent]:
    if limit <= 0:
        return []
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        rows = (
            await s.execute(
                text("""
            UPDATE inbox_events SET status = 'running', lease_owner = :w,
                   lease_until = now() + make_interval(secs => :lease), attempts = attempts + 1
            WHERE id IN (SELECT id FROM inbox_events WHERE status = 'pending' AND run_after <= now()
                         ORDER BY run_after LIMIT :n FOR UPDATE SKIP LOCKED)
            RETURNING id, provider, route_key, dedupe_key, headers, payload, simulated, attempts, trace_context"""),
                {"w": worker, "lease": lease_s, "n": limit},
            )
        ).all()
    return [
        InboxEvent(
            r.id,
            str(r.provider),
            r.route_key,
            r.dedupe_key,
            r.headers,
            r.payload,
            r.simulated,
            r.attempts,
            r.trace_context,
        )
        for r in rows
    ]


async def complete(event: InboxEvent, worker: str, status: str = "done", error: str | None = None) -> None:
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        await s.execute(
            text("""UPDATE inbox_events SET status = CAST(:st AS queue_status), lease_owner = NULL,
                                       last_error = :err WHERE id = :id AND lease_owner = :w"""),
            {"st": status, "err": error, "id": event.id, "w": worker},
        )


async def fail(event: InboxEvent, worker: str, error: str) -> str:
    dead = event.attempts >= MAX_INBOX_ATTEMPTS
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        await s.execute(
            text("""
            UPDATE inbox_events SET status = CAST(:st AS queue_status), lease_owner = NULL, lease_until = NULL,
                   last_error = :err, run_after = now() + make_interval(secs => :delay)
            WHERE id = :id AND lease_owner = :w"""),
            {
                "st": "dead" if dead else "pending",
                "err": error[:2000],
                "delay": backoff_s(event.attempts),
                "id": event.id,
                "w": worker,
            },
        )
    return "dead" if dead else "pending"
