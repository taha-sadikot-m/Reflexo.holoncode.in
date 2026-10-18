"""Generic job queue on the `jobs` table (POC_03 §1, §11.1).

* Enqueue is idempotent per (queue, dedupe_key) while a job is pending/running.
* Claim uses FOR UPDATE SKIP LOCKED with a lease; handlers run outside the claim transaction.
* Failures back off exponentially (2^attempts s, max 60 s); exhausted jobs become `dead` (visible in admin).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.contracts import JobHandler, JobRow
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import IST, utcnow
from reflexo.common.tracing import current_trace_context

log = get_logger(__name__)


def backoff_s(attempts: int) -> int:
    return int(min(2**attempts, 60))


def _local_now() -> datetime:
    return utcnow().astimezone(IST)


def _daily_key(queue: str, day: date) -> str:
    return f"{queue}:{day.isoformat()}"


def _next_run(at: time_of_day, *, after_today: bool) -> datetime:
    """The next run time of a daily job: today at `at` unless it already ran or `after_today`, then tomorrow."""
    now = _local_now()
    today_at = datetime.combine(now.date(), at, tzinfo=IST)
    if after_today:
        return today_at + timedelta(days=1)
    return today_at if today_at > now else now


class Jobs:
    """Implements `JobEnqueuer` and `JobHandlerRegistry`."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}
        self._daily: dict[str, time_of_day] = {}  # queue -> local time (IST) of its daily run

    # -- registry
    def register(self, queue: str, handler: JobHandler) -> None:
        if queue in self._handlers:
            raise RuntimeError(f"handler for {queue} already registered")
        self._handlers[queue] = handler

    def get(self, queue: str) -> JobHandler | None:
        return self._handlers.get(queue)

    def register_daily(self, queue: str, handler: JobHandler, at: str) -> None:
        """A job that runs once a day at `at` (HH:MM, IST); each run enqueues the next one (BETA_12 §6.2)."""
        hh, mm = (int(x) for x in at.split(":"))
        self._daily[queue] = time_of_day(hh, mm)

        async def run_then_schedule(job: JobRow) -> None:
            try:
                await handler(job)
            finally:
                await self._enqueue_daily(queue, _next_run(self._daily[queue], after_today=True))

        self.register(queue, run_then_schedule)

    async def ensure_daily(self) -> None:
        """At worker start: today's run if it has not happened yet (at once, if its time has passed), else
        tomorrow's. The dedupe key per run date keeps restarts and several workers from doubling a run."""
        for queue, at in self._daily.items():
            today = _local_now().date()
            async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
                status = (
                    await s.execute(
                        text("SELECT status FROM jobs WHERE queue = :q AND dedupe_key = :k ORDER BY created_at DESC"),
                        {"q": queue, "k": _daily_key(queue, today)},
                    )
                ).scalar()
            if status is None:
                await self._enqueue_daily(queue, _next_run(at, after_today=False))
            elif str(status) not in ("pending", "running"):  # today's run is over: tomorrow's
                await self._enqueue_daily(queue, _next_run(at, after_today=True))

    async def _enqueue_daily(self, queue: str, when: datetime) -> None:
        delay = max((when - _local_now()).total_seconds(), 0.0)
        await self.enqueue(queue, {"run_date": when.date().isoformat()}, dedupe_key=_daily_key(queue, when.date()),
                           delay_s=delay, priority=PriorityClass.P3)  # fmt: skip

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
    async def claim(
        self,
        worker: str,
        lease_s: int,
        limit: int,
        *,
        only: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> list[JobRow]:
        """Claim due jobs. `only`/`exclude` let a dedicated process own some queues (voice-bridge)."""
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
                               AND (CAST(:only AS text[]) IS NULL OR queue = ANY(CAST(:only AS text[])))
                               AND (CAST(:excl AS text[]) IS NULL OR NOT (queue = ANY(CAST(:excl AS text[]))))
                             ORDER BY priority, run_after LIMIT :n FOR UPDATE SKIP LOCKED)
                RETURNING id, business_id, queue, payload, attempts, max_attempts, trace_context"""),
                    {
                        "w": worker,
                        "lease": lease_s,
                        "n": limit,
                        "only": list(only) if only is not None else None,
                        "excl": list(exclude) if exclude is not None else None,
                    },
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
