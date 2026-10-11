"""Temporal worker process (POC_01 §3): `python -m reflexo.workflows.worker`, task queue `reflexo-main`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from temporalio.worker import Worker

from reflexo.bootstrap import bootstrap
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.logging import get_logger
from reflexo.workflows.client import client, task_queue
from reflexo.workflows.import_activities import ACTIVITIES as IMPORT_ACTIVITIES
from reflexo.workflows.imports import ImportWorkflow

log = get_logger(__name__)
WORKFLOWS = [ImportWorkflow]
ACTIVITIES: list[Callable[..., Any]] = [*IMPORT_ACTIVITIES]
HEARTBEAT_S = 5


async def _heartbeat(worker_id: str, stop: asyncio.Event) -> None:
    started = None
    while not stop.is_set():
        try:
            async with get_db().service_session(ServiceReason.HEALTH) as s:
                started = started or (await s.execute(text("SELECT now()"))).scalar_one()
                await s.execute(
                    text("""INSERT INTO worker_heartbeats (worker_id, service, pools, started_at, beat_at, stats)
                            VALUES (:id, 'temporal-worker', :pools, :started, now(), CAST(:stats AS jsonb))
                            ON CONFLICT (worker_id) DO UPDATE SET beat_at = now()"""),
                    {
                        "id": worker_id,
                        "pools": [task_queue()],
                        "started": started,
                        "stats": json.dumps({"workflows": [w.__name__ for w in WORKFLOWS]}),
                    },
                )
        except Exception:
            log.exception("heartbeat_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)


async def run() -> None:
    worker_id = f"{socket.gethostname()}:temporal:{os.getpid()}"
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C raises KeyboardInterrupt instead
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    worker = Worker(await client(), task_queue=task_queue(), workflows=WORKFLOWS, activities=ACTIVITIES)
    beat = asyncio.create_task(_heartbeat(worker_id, stop))
    log.info(
        "temporal_worker_start", worker=worker_id, task_queue=task_queue(), workflows=[w.__name__ for w in WORKFLOWS]
    )
    async with worker:
        await stop.wait()
    beat.cancel()


def main() -> None:
    bootstrap("reflexo-temporal-worker")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
