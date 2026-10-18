"""Automation runtime shared by the instant executor (job) and the durable interpreter (Temporal activities)
(POC_06 §7). Every side effect is keyed `auto:{version_id}:{trigger_event_id}:{step_id}`, so a retried step never
acts twice; non-waiting steps run here, waits belong to the durable interpreter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
import reflexo.policy.api as policy
from reflexo.automations import cel
from reflexo.automations.guard import _reserve
from reflexo.automations.schema import AutomationDefinition
from reflexo.automations.variables import customer_variables, order_variables
from reflexo.common import ports
from reflexo.common.contracts import OwnerNotifyPort
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)
TRIGGER_BY_EVENT = {
    "message.received": "message_received",
    "comment.received": "comment_received",
    "order.created": "order_created",
    "order.confirmed": "order_confirmed",
    "order.cancelled": "order_cancelled",
    "order.status_changed": "order_status_changed",
    "payment.link_created": "payment_link_created",
    "payment.captured": "payment_captured",
    "payment.failed": "payment_failed",
}


class StepFailed(RuntimeError):
    pass


@dataclass
class RunContext:
    business_id: UUID
    run_id: UUID
    version_id: UUID
    automation_id: UUID
    trigger_event_id: UUID
    tz: str
    variables: dict[str, Any] = field(default_factory=dict)

    def key(self, step_id: str) -> str:
        return f"auto:{self.version_id}:{self.trigger_event_id}:{step_id}"


async def event_variables(
    business_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    customer_id: UUID | None,
    conversation_id: UUID | None,
    text_: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """`event`, `order` (when the event is about one), `customer`, `business` for CEL."""
    settings = await knowledge.business_settings(business_id)
    variables: dict[str, Any] = {
        "event": {
            "type": TRIGGER_BY_EVENT.get(event_type, event_type),
            "customer_id": str(customer_id) if customer_id else None,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "channel": channel,
            "text": text_ or "",
            "payload": payload,
        },
        "customer": await customer_variables(business_id, customer_id),
        "business": {"name": settings.get("name")},
        "vars": {},
        "steps": {},
    }
    order_id = payload.get("order_id")
    if order_id:
        view = await commerce.get_order(business_id, UUID(str(order_id)))
        if view is not None:
            variables["order"] = await order_variables(business_id, view.model_dump(mode="json"))
    return variables


async def refresh_order(business_id: UUID, variables: dict[str, Any]) -> dict[str, Any]:
    """Before acting after a wait: the order as it is now (status, slot may have changed)."""
    order = variables.get("order")
    if not order or not order.get("id"):
        return variables
    view = await commerce.get_order(business_id, UUID(str(order["id"])))
    if view is None:
        return variables
    return {**variables, "order": await order_variables(business_id, view.model_dump(mode="json"))}


async def customer_conversation(business_id: UUID, variables: dict[str, Any]) -> UUID | None:
    """The conversation a customer message goes to: the trigger's, else the customer's most recent one."""
    event = variables.get("event") or {}
    if event.get("conversation_id"):
        return UUID(str(event["conversation_id"]))
    customer_id = (variables.get("customer") or {}).get("id") or event.get("customer_id")
    if not customer_id:
        return None
    async with get_db().tenant_session(business_id) as s:
        found: UUID | None = (
            await s.execute(
                text("""SELECT id FROM conversations WHERE customer_id = :c AND participant_role = 'customer'
                        ORDER BY last_inbound_at DESC NULLS LAST LIMIT 1"""),
                {"c": customer_id},
            )
        ).scalar_one_or_none()
    return found


async def _record(ctx: RunContext, step_id: str, status: str, output: Any = None, error: str | None = None) -> None:
    async with get_db().tenant_session(ctx.business_id) as s:
        await s.execute(
            text("""INSERT INTO automation_run_steps (id, business_id, run_id, step_id, status, idempotency_key,
                                                      output, error, started_at, finished_at)
                    VALUES (:id, :b, :r, :st, CAST(:status AS step_status), :k, CAST(:o AS jsonb),
                            CAST(:e AS jsonb), now(), now())
                    ON CONFLICT (idempotency_key) DO UPDATE SET status = EXCLUDED.status, output = EXCLUDED.output,
                        error = EXCLUDED.error, finished_at = now()"""),
            {
                "id": new_id(),
                "b": ctx.business_id,
                "r": ctx.run_id,
                "st": step_id,
                "status": status,
                "k": ctx.key(step_id),
                "o": json.dumps(output, default=str) if output is not None else None,
                "e": json.dumps({"message": error}) if error else None,
            },
        )


async def _done(ctx: RunContext, step_id: str) -> dict[str, Any] | None:
    async with get_db().tenant_session(ctx.business_id) as s:
        row = (
            await s.execute(
                text("SELECT output FROM automation_run_steps WHERE idempotency_key = :k AND status = 'done'"),
                {"k": ctx.key(step_id)},
            )
        ).first()
    return dict(row.output or {}) if row else None


async def execute_step(ctx: RunContext, d: AutomationDefinition, step_id: str) -> str | None:
    """Run one non-waiting step; returns the next step id (None = end). Idempotent per step key."""
    s: Any = d.step(step_id)
    stored = await _done(ctx, step_id)
    if stored is not None:
        return stored.get("next")
    v, tz = ctx.variables, ctx.tz
    nxt: str | None = getattr(s, "next", None)
    output: dict[str, Any] = {}
    try:
        if s.type == "end":
            nxt = None
        elif s.type == "branch":
            nxt = s.then if cel.evaluate(s.cel, v, tz=tz) else s.else_
        elif s.type == "send":
            output = await _send(ctx, s)
        elif s.type == "notify_owner":
            await _notify_owner(ctx, cel.render(s.text, v, tz=tz), s.priority, step_id)
        elif s.type == "claim_once":
            if not await _claim(ctx, cel.render(s.key, v, tz=tz)):
                nxt = s.goto if s.on_claimed == "goto" and s.goto else None
                output = {"claimed_before": True}
        elif s.type == "counter_limit":
            key = cel.render(s.key, v, tz=tz)
            amount = int(cel.evaluate(s.amount, v, tz=tz))
            order_id = str((v.get("order") or {}).get("id") or ctx.run_id)
            async with get_db().tenant_session(ctx.business_id) as session:
                ok = await _reserve(session, ctx.business_id, ctx.automation_id, key, amount, s.limit, order_id)
            if not ok:
                nxt = s.goto if s.on_exhausted == "goto" else (None if s.on_exhausted == "deny" else nxt)
                output = {"exhausted": True}
        else:
            raise StepFailed(f"step type '{s.type}' is not supported by this executor yet")
    except (StepFailed, cel.CelError) as exc:
        await _record(ctx, step_id, "failed", error=str(exc))
        await emit(
            "workflow_step",
            PipelineEventStatus.ERROR,
            business_id=ctx.business_id,
            step=step_id,
            automation=d.name,
            error=str(exc)[:200],
        )
        raise
    await _record(ctx, step_id, "done", {**output, "next": nxt})
    await emit(
        "workflow_step",
        PipelineEventStatus.OK,
        business_id=ctx.business_id,
        step=step_id,
        type=s.type,
        automation=d.name,
    )
    return nxt


async def _send(ctx: RunContext, s: Any) -> dict[str, Any]:
    v, tz = ctx.variables, ctx.tz
    if s.to == "owner":
        await _notify_owner(ctx, cel.render(s.text or "", v, tz=tz), "p1", s.id)
        return {"to": "owner"}
    conversation_id = await customer_conversation(ctx.business_id, v)
    if conversation_id is None:
        raise StepFailed("no conversation for this customer")
    customer_id = (v.get("customer") or {}).get("id") or (v.get("event") or {}).get("customer_id")
    if not customer_id:
        raise StepFailed("no customer for this message")
    # every step that acts is decided (BETA_05 §2.3); the owner's confirmation of the automation is the approval
    # for the actions it names, while platform limits and later owner rules still apply
    decision = await agent.decide(
        agent.ActionRequest(
            capability="message.send",
            business_id=ctx.business_id,
            requested_by="automation",
            desk="automation",
            customer_id=UUID(str(customer_id)),
            resource=policy.EntityRef(type="Customer", id=str(customer_id)),
            conversation_id=conversation_id,
            owner_approved=True,
        )
    )
    if decision.effect != "act":
        return {"to": "customer", "sent": False, "decision": decision.effect, "policy_ids": decision.policy_ids}
    if s.text:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": cel.render(s.text, v, tz=tz)}]
    else:
        blocks = [{k: (cel.render(x, v, tz=tz) if isinstance(x, str) else x) for k, x in b.items()} for b in s.blocks]
    async with get_db().tenant_session(ctx.business_id) as session:
        result = await gateway.outbound().enqueue(
            session,
            business_id=ctx.business_id,
            conversation_id=conversation_id,
            blocks=blocks,
            origin="automation",
            idempotency_base=ctx.key(s.id),
        )
    return {"to": "customer", "conversation_id": str(conversation_id), "result": str(getattr(result, "status", ""))}


async def _notify_owner(ctx: RunContext, text_: str, priority: str, step_id: str) -> None:
    notifier = ports.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
    if notifier is None:
        raise StepFailed("owner notifications unavailable")
    conv = (ctx.variables.get("event") or {}).get("conversation_id")
    await notifier.notify(
        ctx.business_id,
        "automation",
        text_,
        dedupe_key=ctx.key(step_id),
        priority=PriorityClass(priority),
        about_conversation=UUID(conv) if conv else None,
    )


async def _claim(ctx: RunContext, key: str) -> bool:
    async with get_db().tenant_session(ctx.business_id) as s:
        row = (
            await s.execute(
                text("""INSERT INTO automation_claims (automation_id, business_id, claim_key, run_id)
                        VALUES (:a, :b, :k, :r) ON CONFLICT DO NOTHING RETURNING claim_key"""),
                {"a": ctx.automation_id, "b": ctx.business_id, "k": key, "r": ctx.run_id},
            )
        ).first()
    return row is not None


async def finish_run(business_id: UUID, run_id: UUID, status: str, error: str | None = None) -> None:
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""UPDATE automation_runs SET status = CAST(:st AS automation_run_status), finished_at = now(),
                           error = CAST(:e AS jsonb) WHERE id = :r AND status IN ('running', 'waiting')"""),
            {"st": status, "e": json.dumps({"message": error}) if error else None, "r": run_id},
        )


async def load_run(business_id: UUID, run_id: UUID) -> tuple[RunContext, AutomationDefinition, dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("""SELECT r.id, r.automation_version_id, r.trigger_event_id, r.instance_key, v.definition,
                               v.automation_id
                        FROM automation_runs r JOIN automation_versions v ON v.id = r.automation_version_id
                        WHERE r.id = :r"""),
                {"r": run_id},
            )
        ).one()
    settings = await knowledge.business_settings(business_id)
    ctx = RunContext(
        business_id=business_id,
        run_id=row.id,
        version_id=row.automation_version_id,
        automation_id=row.automation_id,
        trigger_event_id=row.trigger_event_id,
        tz=str(settings.get("timezone") or "Asia/Kolkata"),
    )
    return ctx, AutomationDefinition.model_validate(row.definition), {"instance_key": row.instance_key}
