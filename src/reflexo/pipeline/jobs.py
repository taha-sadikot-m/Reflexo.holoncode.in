"""Generic job queue on the `jobs` table (POC_03 §1, §11.1).

* Enqueue is idempotent per (queue, dedupe_key) while a job is pending/running.
* Claim uses FOR UPDATE SKIP LOCKED with a lease; handlers run outside the claim transaction.
* Failures back off exponentially (2^attempts s, max 60 s); exhausted jobs become `dead` (visible in admin).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.contracts import JobHandler, JobRow
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.tracing import current_trace_context

log = get_logger(__name__)


def backoff_s(attempts: int) -> int:
    return int(min(2**attempts, 60))


class Jobs:
    """Implements `JobEnqueuer` and `JobHandlerRegistry`."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    # -- registry
    def register(self, queue: str, handler: JobHandler) -> None:
        if queue in self._handlers:
            raise RuntimeError(f"handler for {queue} already registered")
        self._handlers[queue] = handler

    def get(self, queue: str) -> JobHandler | None:
        return self._handlers.get(queue)

    def queues(self) -> list[str]:
        return sorted(self._handlers)

    # -- enqueue
    async def enqueue(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        business_id: UUID | None = None,
        priority: PriorityClass = PriorityClass.P3,
        dedupe_key: str | None = None,
        delay_s: float = 0,
        max_attempts: int = 5,
        session: AsyncSession | None = None,
    ) -> UUID | None:
        params = {
            "id": new_id(),
            "b": business_id,
            "q": queue,
            "p": json.dumps(payload, default=str),
            "prio": str(priority),
            "k": dedupe_key,
            "delay": delay_s,
            "max": max_attempts,
            "trace": json.dumps(current_trace_context()),
        }
        sql = text("""
            INSERT INTO jobs (id, business_id, queue, payload, priority, dedupe_key, run_after, max_attempts,
                              trace_context)
            VALUES (:id, :b, :q, CAST(:p AS jsonb), CAST(:prio AS priority_class), :k,
                    now() + make_interval(secs => :delay), :max, CAST(:trace AS jsonb))
            ON CONFLICT (queue, dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running')
            DO NOTHING
            RETURNING id""")
        if session is not None:
            job_id = (await session.execute(sql, params)).scalar_one_or_none()
            await session.execute(text("SELECT pg_notify('q_jobs', :q)"), {"q": queue})
            return job_id
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            job_id = (await s.execute(sql, params)).scalar_one_or_none()
            await s.execute(text("SELECT pg_notify('q_jobs', :q)"), {"q": queue})
            return job_id

    async def cancel(self, queue: str, dedupe_key: str, session: AsyncSession | None = None) -> None:
        sql = text("UPDATE jobs SET status = 'done' WHERE queue = :q AND dedupe_key = :k AND status = 'pending'")
        if session is not None:
            await session.execute(sql, {"q": queue, "k": dedupe_key})
            return
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            await s.execute(sql, {"q": queue, "k": dedupe_key})

    # -- worker side
    async def claim(self, worker: str, lease_s: int, limit: int) -> list[JobRow]:
        if limit <= 0:
            return []
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            rows = (
                await s.execute(
                    text("""
                UPDATE jobs SET status = 'running', lease_owner = :w,
                                lease_until = now() + make_interval(secs => :lease),
                                attempts = attempts + 1
                WHERE id IN (SELECT id FROM jobs WHERE status = 'pending' AND run_after <= now()
                             ORDER BY priority, run_after LIMIT :n FOR UPDATE SKIP LOCKED)
                RETURNING id, business_id, queue, payload, attempts, max_attempts, trace_context"""),
                    {"w": worker, "lease": lease_s, "n": limit},
                )
            ).all()
        return [
            JobRow(r.id, r.business_id, r.queue, r.payload, r.attempts, r.max_attempts, r.trace_context) for r in rows
        ]

    async def complete(self, job: JobRow, worker: str) -> None:
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            await s.execute(
                text("UPDATE jobs SET status = 'done', lease_owner = NULL WHERE id = :id AND lease_owner = :w"),
                {"id": job.id, "w": worker},
            )

    async def fail(self, job: JobRow, worker: str, error: str) -> str:
        """Retry with backoff, or move to `dead`. Returns the new status."""
        dead = job.attempts >= job.max_attempts
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            await s.execute(
                text("""
                UPDATE jobs SET status = CAST(:st AS queue_status), lease_owner = NULL, lease_until = NULL,
                                last_error = :err, run_after = now() + make_interval(secs => :delay)
                WHERE id = :id AND lease_owner = :w"""),
                {
                    "st": "dead" if dead else "pending",
                    "err": error[:2000],
                    "delay": backoff_s(job.attempts),
                    "id": job.id,
                    "w": worker,
                },
            )
        if dead:
            log.error("job_dead", queue=job.queue, job_id=str(job.id), error=error[:300])
        return "dead" if dead else "pending"
