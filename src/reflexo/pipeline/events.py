"""Domain event log (POC_03 §11.2). Appends `domain_events` in the caller's transaction and fans out to
automations via the `automations.route` job, except for message events (routed inline) and `message.sent`
(never an automation trigger). Loop guard: automation-caused events at causation depth ≥ 3 are dropped."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.contracts import NewDomainEvent
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.tracing import current_trace_context
from reflexo.pipeline.jobs import Jobs

log = get_logger(__name__)

NOT_ROUTED = frozenset({"message.received", "comment.received", "message.sent", "message.status"})
MAX_AUTOMATION_DEPTH = 3


class DomainEvents:
    """Implements `DomainEventWriter`."""

    def __init__(self, jobs: Jobs) -> None:
        self._jobs = jobs

    async def append(self, session: AsyncSession, event: NewDomainEvent) -> UUID | None:
        if event.source.startswith("automation") and event.causation_depth >= MAX_AUTOMATION_DEPTH:
            log.warning(
                "domain_event_loop_dropped",
                type=event.type,
                depth=event.causation_depth,
                business_id=str(event.business_id),
            )
            return None
        event_id = new_id()
        await session.execute(
            text("""
            INSERT INTO domain_events (id, business_id, type, occurred_at, source, customer_id, conversation_id,
                                       correlation_id, causation_id, causation_depth, payload, trace_context)
            VALUES (:id, :b, :t, COALESCE(:at, now()), :src, :cust, :conv, :corr, :cause, :depth,
                    CAST(:p AS jsonb), CAST(:trace AS jsonb))"""),
            {
                "id": event_id,
                "b": event.business_id,
                "t": event.type,
                "at": event.occurred_at,
                "src": event.source,
                "cust": event.customer_id,
                "conv": event.conversation_id,
                "corr": event.correlation_id,
                "cause": event.causation_id,
                "depth": event.causation_depth,
                "p": json.dumps(event.payload, default=str),
                "trace": json.dumps(current_trace_context()),
            },
        )
        if event.type not in NOT_ROUTED and self._jobs.get("automations.route") is not None:
            await self._jobs.enqueue(
                "automations.route",
                {"event_id": str(event_id)},
                business_id=event.business_id,
                priority=PriorityClass.P2,
                dedupe_key=f"route:{event_id}",
                session=session,
            )
        return event_id
