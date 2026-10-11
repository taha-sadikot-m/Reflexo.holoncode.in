"""Public interface of the `pipeline` module. Other modules import only from here (POC_01 §4).

Queue mechanics, worker runtime, turn scheduling (coalescing, supersession), jobs, domain events.
Spec: POC_03 §1, §4, §7, §11.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from reflexo.common.contracts import (
    DomainEventWriter,
    JobEnqueuer,
    JobHandlerRegistry,
    JobRow,
    TurnRunnerPort,
    TurnScheduler,
)
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ports import Registry
from reflexo.pipeline.events import DomainEvents
from reflexo.pipeline.inbox import StoreResult
from reflexo.pipeline.inbox import store as store_inbox_event
from reflexo.pipeline.jobs import Jobs
from reflexo.pipeline.outbound import confirm_by_provider_id
from reflexo.pipeline.turns import HOLDING_QUEUE, TurnService

__all__ = ["StoreResult", "confirm_by_provider_id", "register", "store_inbox_event"]


def register(registry: Registry) -> None:
    jobs = Jobs()
    turns = TurnService(jobs)
    events = DomainEvents(jobs)
    registry.provide(Jobs, jobs)
    registry.provide(TurnService, turns)
    registry.provide(JobEnqueuer, jobs)  # type: ignore[type-abstract]
    registry.provide(JobHandlerRegistry, jobs)  # type: ignore[type-abstract]
    registry.provide(DomainEventWriter, events)  # type: ignore[type-abstract]
    registry.provide(TurnScheduler, turns)  # type: ignore[type-abstract]

    async def holding_check(job: JobRow) -> None:
        """12 s after a turn started: if it is still running and has not replied, send the holding message."""
        turn_id = UUID(job.payload["turn_id"])
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            row = (
                await s.execute(
                    text("""SELECT business_id, conversation_id, status, superseded_by
                            FROM conversation_turns WHERE id = :id"""),
                    {"id": turn_id},
                )
            ).first()
        if row is None or row.status != "running" or row.superseded_by is not None:
            return
        runner = registry.optional(TurnRunnerPort)  # type: ignore[type-abstract]
        if runner is not None:
            await runner.send_holding_reply(row.business_id, row.conversation_id, turn_id)

    jobs.register(HOLDING_QUEUE, holding_check)
