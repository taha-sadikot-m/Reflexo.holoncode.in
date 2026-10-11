"""Router (POC_06 §5): matches events to waiting runs first, then to active automations, and dispatches runs.

* Inline for inbound customer messages (`route_inbound`, returns consumed/observed/none) — a COD "haan" that a
  waiting run consumes is not also answered by the AI.
* Job `automations.route` for domain events (order.*, payment.*).
* Dispatch: one `automation_runs` row per (version, trigger event) — duplicates are no-ops; instant runs become an
  `automations.instant_step` job, durable runs a Temporal `AutomationRunWorkflow`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.automations import cel
from reflexo.automations.runtime import (
    TRIGGER_BY_EVENT,
    RunContext,
    event_variables,
    execute_step,
    finish_run,
    load_run,
)
from reflexo.automations.store import active_versions
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer, JobRow, RoutableEvent, WorkflowPort
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)
ROUTE_QUEUE = "automations.route"
INSTANT_QUEUE = "automations.instant_step"
WAIT_KEY = {"message_received": "customer", "payment_captured": "order", "order_status_changed": "order"}


class AutomationRouter:
    """EventRouterPort + job handlers."""

    async def route_inbound(self, event: RoutableEvent) -> str:
        variables = await event_variables(
            event.business_id,
            event.type,
            {"post_id": event.post_id, "comment_id": event.comment_id, "option": event.interactive_option_id},
            customer_id=event.customer_id,
            conversation_id=event.conversation_id,
            text_=event.text,
            channel=event.channel,
        )
        return await self.route(event.business_id, event.type, event.domain_event_id or event.message_id, variables)

    async def route_job(self, job: JobRow) -> None:
        business_id = job.business_id
        assert business_id is not None
        async with get_db().tenant_session(business_id) as s:
            row = (
                await s.execute(
                    text("""SELECT id, type, payload, customer_id, conversation_id FROM domain_events WHERE id = :e"""),
                    {"e": UUID(job.payload["event_id"])},
                )
            ).first()
        if row is None or row.type not in TRIGGER_BY_EVENT:
            return
        variables = await event_variables(
            business_id,
            row.type,
            dict(row.payload or {}),
            customer_id=row.customer_id,
            conversation_id=row.conversation_id,
        )
        await self.route(business_id, row.type, row.id, variables)

    async def route(self, business_id: UUID, event_type: str, event_id: UUID, variables: dict[str, Any]) -> str:
        trigger_type = TRIGGER_BY_EVENT.get(event_type)
        if trigger_type is None:
            return "none"
        consumed = await self._deliver_to_waits(business_id, trigger_type, variables)
        matched, consume_mode = await self._dispatch(business_id, trigger_type, event_id, variables)
        decision = "consumed" if consumed or consume_mode else "observed" if matched else "none"
        if decision != "none":
            await emit(
                "automation_route",
                PipelineEventStatus.OK,
                business_id=business_id,
                event=event_type,
                matched=matched,
                decision=decision,
            )
        return decision

    async def _deliver_to_waits(self, business_id: UUID, trigger_type: str, variables: dict[str, Any]) -> bool:
        kind = WAIT_KEY.get(trigger_type)
        if kind is None:
            return False
        key = (
            (variables.get("customer") or {}).get("id")
            if kind == "customer"
            else (variables.get("order") or {}).get("id")
        )
        if not key:
            return False
        async with get_db().tenant_session(business_id) as s:
            waits = (
                await s.execute(
                    text("""SELECT id, workflow_id, match_cel, consume FROM automation_waits
                            WHERE status = 'waiting' AND event_type = :t AND correlation_key = :k AND expires_at > now()
                            ORDER BY created_at"""),
                    {"t": trigger_type, "k": str(key)},
                )
            ).all()
        consumed = False
        settings_tz = "Asia/Kolkata"
        for w in waits:
            try:
                if not cel.evaluate(w.match_cel, variables, tz=settings_tz):
                    continue
            except cel.CelError as exc:
                log.warning("wait_match_failed", wait=str(w.id), error=str(exc)[:200])
                continue
            async with get_db().tenant_session(business_id) as s:
                won = (
                    await s.execute(
                        text(
                            "UPDATE automation_waits SET status = 'matched' WHERE id = :id AND status = 'waiting' "
                            "RETURNING id"
                        ),
                        {"id": w.id},
                    )
                ).first()
            if won is None:
                continue
            await ports.registry.require(WorkflowPort).signal(  # type: ignore[type-abstract]
                w.workflow_id, "event", {"wait_id": str(w.id), "event": variables.get("event", {})}
            )
            consumed = consumed or bool(w.consume)
        return consumed

    async def _dispatch(
        self, business_id: UUID, trigger_type: str, event_id: UUID, variables: dict[str, Any]
    ) -> tuple[int, bool]:
        versions = [v for v in await active_versions(business_id) if v.executor in ("instant", "durable")]
        event = variables.get("event") or {}
        words = set(str(event.get("text") or "").lower().split())
        matched, consume = 0, False
        settings = get_settings()
        for v in versions:
            idx, d = v.trigger_index, v.definition
            if idx.get("type") != trigger_type:
                continue
            if idx.get("channels") and event.get("channel") not in idx["channels"]:
                continue
            if idx.get("keywords") and not words & set(idx["keywords"]):
                continue
            if idx.get("to_status") and (event.get("payload") or {}).get("to_status") not in idx["to_status"]:
                continue
            try:
                if d.trigger.cel and not cel.evaluate(d.trigger.cel, variables):
                    continue
                instance_key = cel.render(d.instance_key, variables)
            except cel.CelError as exc:
                log.warning("automation_trigger_failed", automation=v.name, error=str(exc)[:200])
                continue
            run_id = await self._create_run(business_id, v.version_id, event_id, instance_key, v.executor)
            if run_id is None:
                continue  # this event already started this automation
            matched += 1
            consume = consume or d.mode == "consume"
            if v.executor == "instant":
                await ports.registry.require(JobEnqueuer).enqueue(  # type: ignore[type-abstract]
                    INSTANT_QUEUE,
                    {"run_id": str(run_id), "variables": variables},
                    business_id=business_id,
                    priority=PriorityClass.P2,
                    dedupe_key=f"instant:{run_id}",
                )
            else:
                started = await ports.registry.require(WorkflowPort).start_automation_run(  # type: ignore[type-abstract]
                    business_id,
                    run_id=run_id,
                    version_id=v.version_id,
                    instance_key=instance_key,
                    variables=variables,
                    time_scale=settings.demo_time_scale,
                )
                if not started:
                    await finish_run(business_id, run_id, "cancelled", "already running for this instance")
        return matched, consume

    async def _create_run(
        self, business_id: UUID, version_id: UUID, event_id: UUID, instance_key: str, executor: str
    ) -> UUID | None:
        async with get_db().tenant_session(business_id) as s:
            created: UUID | None = (
                await s.execute(
                    text("""INSERT INTO automation_runs (id, business_id, automation_version_id, instance_key,
                                                         trigger_event_id, status, workflow_id)
                            VALUES (:id, :b, :v, :k, :e, 'running', :w)
                            ON CONFLICT (automation_version_id, trigger_event_id) DO NOTHING RETURNING id"""),
                    {
                        "id": (run_id := new_id()),
                        "b": business_id,
                        "v": version_id,
                        "k": instance_key,
                        "e": event_id,
                        "w": f"auto-{version_id}-{instance_key}" if executor == "durable" else None,
                    },
                )
            ).scalar_one_or_none()
        return created if created == run_id else None

    async def instant_job(self, job: JobRow) -> None:
        """`automations.instant_step`: walk an instant run to the end (every step idempotent)."""
        business_id = job.business_id
        assert business_id is not None
        run_id = UUID(job.payload["run_id"])
        ctx, d, _ = await load_run(business_id, run_id)
        await self.run_steps(ctx, d, job.payload.get("variables") or {})

    async def run_steps(self, ctx: RunContext, d: Any, variables: dict[str, Any]) -> None:
        ctx.variables = variables
        step: str | None = d.steps[0].id
        try:
            while step:
                step = await execute_step(ctx, d, step)
        except Exception as exc:
            await finish_run(ctx.business_id, ctx.run_id, "failed", str(exc)[:500])
            raise
        await finish_run(ctx.business_id, ctx.run_id, "completed")
