"""Worker process (POC_03 §1): pools `inbound`, `turns`, `outbound`, `jobs` plus a lease sweeper.

Wake-ups come from Postgres LISTEN/NOTIFY with a polling fallback, so a lost notification only adds latency.
Each pool claims at most its free concurrency slots, runs handlers as tasks, and never lets a handler error
kill the loop. Run: `python -m reflexo.pipeline.worker --pools all`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    InboundProcessorPort,
    InboxEvent,
    LeaseLost,
    OutboundRow,
    OutboundSenderPort,
    SendResult,
    SendStatus,
    Superseded,
    TurnRef,
    TurnRunnerPort,
)
from reflexo.common.db import ServiceReason, get_db, measure_db, plain_dsn
from reflexo.common.enums import PipelineEventStatus, TurnOutcome
from reflexo.common.flags import business_killed
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.ratelimit import RateLimiter
from reflexo.common.timeutil import utcnow
from reflexo.common.tracing import resume_trace, tracer
from reflexo.common.xray import emit
from reflexo.pipeline import inbox, outbound
from reflexo.pipeline.jobs import Jobs
from reflexo.pipeline.turns import PipelineTurnHandle, TurnService

log = get_logger(__name__)

POOLS = ("inbound", "turns", "outbound", "jobs")
HEARTBEAT_S = 5.0
# queues owned by a dedicated process (the voice-bridge holds live WebRTC calls in memory)
DEDICATED_QUEUES = ("voice.call_signal",)
CHANNELS = {"inbound": "q_inbound", "turns": "q_turns", "outbound": "q_outbound", "jobs": "q_jobs"}
SWEEP_INTERVAL_S = 5.0
WARM_CONNECTIONS = 4  # per engine, opened before the first claim (cold connections blew the speculative budget)


@dataclass
class PoolConfig:
    concurrency: int
    lease_s: int


def _dsn(sqlalchemy_url: str) -> str:
    return plain_dsn(sqlalchemy_url)


class Worker:
    def __init__(self, registry: Registry, pools: tuple[str, ...] = POOLS) -> None:
        s = get_settings()
        self.id = f"{socket.gethostname()}:{os.getpid()}:{new_id().hex[-6:]}"
        self.pools = pools
        self.registry = registry
        self.jobs: Jobs = registry.require(Jobs)
        self.turns: TurnService = registry.require(TurnService)
        self.config = {
            "inbound": PoolConfig(8, 30),
            "turns": PoolConfig(6, s.turn_lease_s),
            "outbound": PoolConfig(8, 30),
            "jobs": PoolConfig(6, 60),
        }
        self.rate = RateLimiter(s.outbound_rate_per_s)
        self._events = {p: asyncio.Event() for p in pools}
        self._slots = {p: asyncio.Semaphore(self.config[p].concurrency) for p in pools}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()
        self._listen_conn: asyncpg.Connection | None = None

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        started = time.perf_counter()
        await get_db().warm(WARM_CONNECTIONS)
        log.info(
            "worker_start", worker=self.id, pools=list(self.pools), warm_ms=int((time.perf_counter() - started) * 1000)
        )
        await self._listen()
        if "jobs" in self.pools:
            await self.jobs.ensure_daily()  # the next run of every daily job (fx refresh, cost rollup)
        loops = [asyncio.create_task(self._pool_loop(p)) for p in self.pools]
        loops.append(asyncio.create_task(self._sweeper()))
        loops.append(asyncio.create_task(self._heartbeat()))
        await self._stop.wait()
        for t in loops:
            t.cancel()
        await self.drain(timeout_s=15)
        if self._listen_conn is not None:
            await self._listen_conn.close()
        log.info("worker_stopped", worker=self.id)

    def stop(self) -> None:
        self._stop.set()

    async def _heartbeat(self) -> None:
        """Liveness for the admin Overview: one row per worker, refreshed every few seconds."""
        started = utcnow()
        while not self._stop.is_set():
            try:
                async with get_db().service_session(ServiceReason.HEALTH) as s:
                    await s.execute(
                        text("""INSERT INTO worker_heartbeats (worker_id, service, pools, started_at, beat_at, stats)
                                VALUES (:id, 'worker', :pools, :started, now(), CAST(:stats AS jsonb))
                                ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), stats = EXCLUDED.stats"""),
                        {
                            "id": self.id,
                            "pools": list(self.pools),
                            "started": started,
                            "stats": json.dumps({"in_flight": len(self._tasks)}),
                        },
                    )
                    await s.execute(text("DELETE FROM worker_heartbeats WHERE beat_at < now() - interval '1 hour'"))
            except Exception:
                log.exception("heartbeat_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_S)

    async def drain(self, timeout_s: float) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout_s)

    async def _listen(self) -> None:
        try:
            self._listen_conn = await asyncpg.connect(_dsn(get_settings().database_service_url))
            for pool in self.pools:
                channel = CHANNELS[pool]
                await self._listen_conn.add_listener(channel, lambda *_a, p=pool: self._events[p].set())
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("listen_unavailable_polling_only", error=type(exc).__name__)

    async def _pool_loop(self, pool: str) -> None:
        tick = getattr(self, f"tick_{pool}")
        interval = get_settings().poll_interval_s
        while not self._stop.is_set():
            try:
                await tick()
            except Exception as exc:  # a pool loop must survive any single failure
                log.exception("pool_tick_failed", pool=pool, error=type(exc).__name__)
            self._events[pool].clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._events[pool].wait(), timeout=interval)

    def _free(self, pool: str) -> int:
        sem = self._slots[pool]
        return sem._value

    async def _spawn(self, pool: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
        await self._slots[pool].acquire()

        async def runner() -> None:
            try:
                await coro_factory()
            finally:
                self._slots[pool].release()

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ inbound
    async def tick_inbound(self) -> None:
        processor = self.registry.optional(InboundProcessorPort)  # type: ignore[type-abstract]
        if processor is None:
            return
        for event in await inbox.claim(self.id, self.config["inbound"].lease_s, self._free("inbound")):
            await self._spawn("inbound", functools.partial(self._process_inbox, processor, event))

    async def _process_inbox(self, processor: InboundProcessorPort, event: InboxEvent) -> None:
        with resume_trace(event.trace_context), tracer().start_as_current_span("reflexo.inbound"):
            try:
                await processor.process_inbox_event(event)
                await inbox.complete(event, self.id)
            except Exception as exc:
                status = await inbox.fail(event, self.id, f"{type(exc).__name__}: {exc}")
                log.exception("inbox_failed", inbox_id=str(event.id), status=status)

    # ------------------------------------------------------------------ turns
    async def tick_turns(self) -> None:
        runner = self.registry.optional(TurnRunnerPort)  # type: ignore[type-abstract]
        if runner is None:
            return
        for ref in await self.turns.claim_due(self.id, self._free("turns")):
            await self._spawn("turns", functools.partial(self._run_turn, runner, ref))

    async def _run_turn(self, runner: TurnRunnerPort, ref: TurnRef) -> None:
        handle = PipelineTurnHandle(ref, self.id, self.turns)
        started = time.perf_counter()
        with (
            resume_trace(ref.trace_context),
            tracer().start_as_current_span("reflexo.turn", attributes={"reflexo.turn_id": str(ref.turn_id)}),
            measure_db() as db_stats,  # BETA_12 §2: database work per turn
        ):
            await emit(
                "turn_started",
                PipelineEventStatus.START,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                turn_id=ref.turn_id,
                attempt=ref.attempts,
                coalesce_wait_ms=await self._coalesce_wait_ms(ref),
            )
            try:
                await runner.run_turn(handle)
            except Superseded:
                await self.turns.mark(ref, "superseded")
                await emit(
                    "turn_superseded",
                    PipelineEventStatus.SKIP,
                    business_id=ref.business_id,
                    conversation_id=ref.conversation_id,
                    turn_id=ref.turn_id,
                )
                return
            except LeaseLost:
                log.warning("turn_lease_lost", turn_id=str(ref.turn_id))
                return
            except Exception as exc:
                log.exception("turn_failed", turn_id=str(ref.turn_id), attempt=ref.attempts)
                if ref.attempts >= get_settings().turn_max_attempts:
                    await self.turns.mark(ref, "failed", TurnOutcome.FALLBACK)
                    await self._fallback(runner, ref, f"{type(exc).__name__}: {exc}")
                else:
                    await self.turns.release_for_retry(ref, self.id)
                await emit(
                    "turn_error",
                    PipelineEventStatus.ERROR,
                    business_id=ref.business_id,
                    conversation_id=ref.conversation_id,
                    turn_id=ref.turn_id,
                    error=type(exc).__name__,
                )
                return
            limit = get_settings().turn_max_db_queries
            await emit(
                "turn_finished",
                PipelineEventStatus.OK,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                turn_id=ref.turn_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                db_queries=db_stats.queries,
                db_ms=round(db_stats.ms, 1),
                db_over_limit=db_stats.queries > limit,
            )

    async def _coalesce_wait_ms(self, ref: TurnRef) -> int | None:
        """From the turn's last inbound message to its start: how long coalescing held it (BETA_12 §2)."""
        try:
            async with get_db().tenant_session(ref.business_id) as s:
                last = (
                    await s.execute(
                        text("SELECT max(created_at) FROM messages WHERE id = ANY(:ids)"),
                        {"ids": list(ref.message_ids)},
                    )
                ).scalar_one_or_none()
        except Exception:  # measurement never blocks a turn
            return None
        return max(int((utcnow() - last).total_seconds() * 1000), 0) if last else None

    async def _fallback(self, runner: TurnRunnerPort, ref: TurnRef, reason: str) -> None:
        try:
            await runner.run_fallback(ref, reason)
        except Exception:
            log.exception("turn_fallback_failed", turn_id=str(ref.turn_id))

    # ------------------------------------------------------------------ outbound
    async def tick_outbound(self) -> None:
        sender = self.registry.optional(OutboundSenderPort)  # type: ignore[type-abstract]
        if sender is None:
            return
        for row in await outbound.claim(self.id, self.config["outbound"].lease_s, self._free("outbound")):
            await self._spawn("outbound", functools.partial(self._send, sender, row))

    async def _send(self, sender: OutboundSenderPort, row: OutboundRow) -> None:
        with resume_trace(row.trace_context), tracer().start_as_current_span("reflexo.outbound"):
            if await business_killed(row.business_id):
                await outbound.block(row, self.id, "KILL_SWITCH_ACTIVE")
                return
            if not await self.rate.try_acquire(f"out:{row.channel_connection_id}"):
                await outbound.defer(row, self.id, 0.2)
                return
            started = time.perf_counter()
            try:
                result = await sender.send(row)
            except Exception as exc:  # an adapter bug must not lose the message: retry with backoff
                log.exception("outbound_send_crashed", outbound_id=str(row.id))
                result = SendResult(SendStatus.RETRYABLE, error_code="INTERNAL", error_message=type(exc).__name__)
            status = await outbound.apply_result(row, self.id, result)
            await emit(
                "sent" if status == "sent" else "outbound_" + status,
                PipelineEventStatus.OK if status == "sent" else PipelineEventStatus.ERROR,
                business_id=row.business_id,
                conversation_id=row.conversation_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                part=row.part_index,
                provider_message_id=result.provider_message_id,
                error=result.error_code,
            )
            if status == "failed":
                try:
                    await sender.on_final_failure(row, result)
                except Exception:
                    log.exception("outbound_final_failure_hook_failed", outbound_id=str(row.id))

    # ------------------------------------------------------------------ jobs
    async def tick_jobs(self) -> None:
        for job in await self.jobs.claim(
            self.id, self.config["jobs"].lease_s, self._free("jobs"), exclude=DEDICATED_QUEUES
        ):
            await self._spawn("jobs", functools.partial(self._run_job, job))

    async def _run_job(self, job: Any) -> None:
        handler = self.jobs.get(job.queue)
        with (
            resume_trace(job.trace_context),
            tracer().start_as_current_span("reflexo.job", attributes={"reflexo.queue": job.queue}),
        ):
            if handler is None:
                await self.jobs.fail(job, self.id, f"no handler registered for {job.queue}")
                return
            try:
                await handler(job)
                await self.jobs.complete(job, self.id)
            except Exception as exc:
                status = await self.jobs.fail(job, self.id, f"{type(exc).__name__}: {exc}")
                log.exception("job_failed", queue=job.queue, job_id=str(job.id), status=status)

    # ------------------------------------------------------------------ sweeper
    async def sweep_once(self) -> None:
        runner = self.registry.optional(TurnRunnerPort)  # type: ignore[type-abstract]
        for ref in await self.turns.sweep_expired():
            if runner is not None:
                await self._fallback(runner, ref, "worker lost; attempts exhausted")
        await outbound.sweep_expired()
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            # A row whose worker died is retried; one that keeps killing workers ends up in dead letters.
            await s.execute(
                text("""
                UPDATE jobs SET status = CASE WHEN attempts >= max_attempts THEN 'dead'::queue_status
                                              ELSE 'pending'::queue_status END,
                       lease_owner = NULL, lease_until = NULL, last_error = 'worker lost (lease expired)'
                WHERE status = 'running' AND lease_until < now()""")
            )
            await s.execute(
                text("""
                UPDATE inbox_events SET status = CASE WHEN attempts >= :max THEN 'dead'::queue_status
                                                      ELSE 'pending'::queue_status END,
                       lease_owner = NULL, lease_until = NULL, last_error = 'worker lost (lease expired)'
                WHERE status = 'running' AND lease_until < now()"""),
                {"max": inbox.MAX_INBOX_ATTEMPTS},
            )

    async def _sweeper(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep_once()
            except Exception:
                log.exception("sweep_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_INTERVAL_S)


def main() -> None:
    from reflexo.bootstrap import bootstrap

    parser = argparse.ArgumentParser()
    parser.add_argument("--pools", default="all", help="comma list of pools or 'all'")
    args = parser.parse_args()
    pools = POOLS if args.pools == "all" else tuple(p.strip() for p in args.pools.split(","))

    async def _run() -> None:
        registry = bootstrap("reflexo-worker")
        worker = Worker(registry, pools)
        try:
            await worker.run()
        except asyncio.CancelledError:
            worker.stop()

    with contextlib.suppress(KeyboardInterrupt):
        # psycopg (LangGraph checkpointer) cannot run on Windows' default Proactor loop
        asyncio.run(_run(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


if __name__ == "__main__":
    main()
