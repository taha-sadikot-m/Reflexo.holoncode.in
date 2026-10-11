"""`voice-bridge` process (POC_01 §3, §5.3): `python -m reflexo.voice.bridge_worker`.

Owns live WhatsApp WebRTC calls. Claims only `voice.call_signal` jobs (the pipeline worker excludes that queue),
woken by Redis `ctl:voice` (fast path), Postgres NOTIFY `q_jobs`, or a 300 ms poll. On start it recovers sessions
it owned before a crash (`bridge_restart`); on stop it ends live calls cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket

import asyncpg
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import JobQueuePort, JobRow
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.logging import get_logger
from reflexo.common.redis import get_redis
from reflexo.common.timeutil import utcnow
from reflexo.common.tracing import resume_trace, tracer
from reflexo.voice.call_controller import CallController

log = get_logger(__name__)
QUEUE = "voice.call_signal"
POLL_S = 0.3
LEASE_S = 60


def host_id() -> str:
    """Stable across restarts of the same bridge instance, so it can find the calls it owned (restart recovery)."""
    return os.environ.get("REFLEXO_VOICE_BRIDGE_ID") or f"{socket.gethostname()}:voice-bridge"


class BridgeWorker:
    def __init__(self, controller: CallController, jobs: JobQueuePort) -> None:
        self.controller = controller
        self.jobs = jobs
        self.worker_id = f"{host_id()}:{os.getpid()}"
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        recovered = await self.controller.recover()
        log.info("voice_bridge_start", worker=self.worker_id, recovered=recovered)
        loops = [asyncio.create_task(c) for c in (self._redis_wake(), self._pg_wake(), self._heartbeat())]
        try:
            while not self._stop.is_set():
                for job in await self.jobs.claim(self.worker_id, LEASE_S, 4, only=[QUEUE]):
                    task = asyncio.create_task(self._run_job(job))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_S)
        finally:
            for t in loops:
                t.cancel()
            await self.controller.shutdown()
            if self._tasks:
                await asyncio.wait(self._tasks, timeout=10)
            log.info("voice_bridge_stopped", worker=self.worker_id)

    async def _run_job(self, job: JobRow) -> None:
        with resume_trace(job.trace_context), tracer().start_as_current_span("voice.call_signal"):
            try:
                await self.controller.handle_signal(job.payload)
                await self.jobs.complete(job, self.worker_id)
            except Exception as exc:
                log.exception("call_signal_failed", job_id=str(job.id))
                await self.jobs.fail(job, self.worker_id, f"{type(exc).__name__}: {exc}"[:500])

    async def _redis_wake(self) -> None:
        """Fast path: the inbound worker publishes `ctl:voice` together with the durable job."""
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = get_redis().client.pubsub()
                await pubsub.subscribe("ctl:voice")
                while not self._stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is not None:
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:  # Redis down: NOTIFY + polling still deliver the job
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _pg_wake(self) -> None:
        dsn = get_settings().database_service_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        while not self._stop.is_set():
            try:
                conn = await asyncpg.connect(dsn)
                await conn.add_listener("q_jobs", lambda *_a: self._wake.set())
                try:
                    await self._stop.wait()
                finally:
                    await conn.close()
            except asyncio.CancelledError:
                raise
            except (OSError, asyncpg.PostgresError):
                await asyncio.sleep(2.0)

    async def _heartbeat(self) -> None:
        started = utcnow()
        while not self._stop.is_set():
            try:
                async with get_db().service_session(ServiceReason.HEALTH) as s:
                    await s.execute(
                        text("""INSERT INTO worker_heartbeats (worker_id, service, pools, started_at, beat_at, stats)
                                VALUES (:id, 'voice-bridge', '{voice}', :st, now(), CAST(:stats AS jsonb))
                                ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), stats = EXCLUDED.stats"""),
                        {
                            "id": self.worker_id,
                            "st": started,
                            "stats": json.dumps({"live_calls": len(self.controller.live)}),
                        },
                    )
            except Exception:
                log.exception("voice_bridge_heartbeat_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)


def main() -> None:
    from reflexo.bootstrap import bootstrap

    async def _run() -> None:
        registry = bootstrap("reflexo-voice-bridge")
        jobs = registry.require(JobQueuePort)  # type: ignore[type-abstract]
        worker = BridgeWorker(CallController(host_id()), jobs)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows: no signal handlers on the loop
                loop.add_signal_handler(sig, worker.stop)
        try:
            await worker.run()
        except asyncio.CancelledError:
            worker.stop()

    with contextlib.suppress(KeyboardInterrupt):
        # default loop on purpose: LiveKit RTC media fails on Windows' selector loop (verified 2026-10-11)
        asyncio.run(_run())


if __name__ == "__main__":
    main()
