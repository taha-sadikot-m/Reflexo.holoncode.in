"""Capabilities and the decision loop (ADR-015, POC_10 §3, §5, §6).

A capability is something the employee can physically do (cancel an order, …). It carries only invariants —
facts about the world — and a day-one default from its risk class. *Whether* this business wants it done is the
playbook's answer (`knowledge.playbook.match`); when nothing covers the request the agent proposes the exact action
to the owner, who decides, and the decision is carried out exactly as proposed (`ProposalService`).

Nothing here knows about any one business: a cancellation policy, a "COD needs reconfirmation", a "custom cakes need
my OK" all live in the playbook as data the owner taught.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.approvals.api as approvals
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent.tools.base import ToolContext, ToolError
from reflexo.common import ports
from reflexo.common.contracts import OwnerNotifyPort
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.xray import emit

log = get_logger(__name__)
Risk = Literal["routine", "commitment", "money"]
Effect = Literal["allow", "deny", "propose"]
PROPOSAL_KIND = "tool_call"  # approval_requests.kind for agent-proposed actions
PROPOSAL_TTL_MIN = 24 * 60


@dataclass(frozen=True)
class Capability:
    name: str
    risk: Risk
    label: str  # how the owner reads it: "Cancelling an order"
    verb: str  # the owner's approve button: "✅ Yes, cancel"
    default: Callable[[dict[str, Any]], Effect]  # day-one common sense, from the situation
    invariant: Callable[[dict[str, Any]], str | None]  # why it is impossible, or None
    execute: Callable[[UUID, dict[str, Any], str], Awaitable[dict[str, Any]]]  # (business, args, by) → outcome


def _by_risk(risk: Risk) -> Effect:
    return "allow" if risk == "routine" else "propose"


# ---------------------------------------------------------------- order.cancel


async def _cancel(business_id: UUID, args: dict[str, Any], by: str) -> dict[str, Any]:
    r = await commerce.cancel_order(
        business_id, UUID(args["order_id"]), by=by, reason=args.get("reason"), notify_owner=False
    )
    out: dict[str, Any] = {"status": r.status, "order_number": r.order.order_number}
    if r.refund_due_paise:
        out["refund_due"] = format_inr(r.refund_due_paise)
    return out


async def _change(business_id: UUID, args: dict[str, Any], by: str) -> dict[str, Any]:
    v = await commerce.change_order(
        business_id, UUID(args["order_id"]), dict(args["changes"]), by=by, notify_owner=False
    )
    return {
        "status": v.status,
        "order_number": v.order_number,
        "delivery": " ".join(x for x in (v.delivery_date or "", v.delivery_slot or "") if x),
        "total": format_inr(v.total_paise),
    }


async def _refund(business_id: UUID, args: dict[str, Any], by: str) -> dict[str, Any]:
    return await commerce.refund_order(business_id, UUID(args["order_id"]), by=by, reason=args.get("reason"))


def _nothing_to_refund(sit: dict[str, Any]) -> str | None:
    return None if sit.get("order.payment_status") == "paid" else "nothing was paid online for this order"


def _not_changeable(sit: dict[str, Any]) -> str | None:
    status = str(sit.get("order.status"))
    return "it was cancelled" if status == "cancelled" else commerce.NOT_CANCELLABLE.get(status)


CAPABILITIES: dict[str, Capability] = {
    "order.cancel": Capability(
        name="order.cancel",
        risk="commitment",
        label="Cancelling an order",
        verb="cancel",
        # a draft is not a promise yet; anything placed changes what the shop committed to
        default=lambda sit: "allow" if sit.get("order.status") == "draft" else _by_risk("commitment"),
        invariant=lambda sit: commerce.NOT_CANCELLABLE.get(str(sit.get("order.status"))),
        execute=_cancel,
    ),
    "order.change": Capability(
        name="order.change",
        risk="commitment",
        label="Changing a placed order",
        verb="change",
        default=lambda sit: "allow" if sit.get("order.status") == "draft" else _by_risk("commitment"),
        invariant=_not_changeable,
        execute=_change,
    ),
    "refund.request": Capability(
        name="refund.request",
        risk="money",
        label="Refunding an order",
        verb="refund",
        default=lambda sit: _by_risk("money"),
        invariant=_nothing_to_refund,
        execute=_refund,
    ),
}


def describe_order(o: commerce.OrderView) -> str:
    items = ", ".join(f"{ln.quantity} × {ln.name}" if ln.quantity > 1 else ln.name for ln in o.lines)
    when = " ".join(x for x in (o.delivery_date or "", o.delivery_slot or "") if x)
    pay = {"cod": "COD", "razorpay_link": "online"}.get(o.payment_method or "", "")
    facts = [f"{format_inr(o.total_paise)}{' ' + pay if pay else ''}", when, o.status.replace("_", " ")]
    return f"{o.order_number} ({items}; " + ", ".join(f for f in facts if f) + ")"


# ---------------------------------------------------------------- the decision loop


async def request(
    ctx: ToolContext,
    s: AsyncSession,
    name: str,
    args: dict[str, Any],
    *,
    reason: str,
    note_for_owner: str,
) -> dict[str, Any]:
    """Invariants → playbook → authority default; then act, decline or propose. Returns the tool result the model
    turns into words for the customer — it never decides the outcome itself."""
    cap = CAPABILITIES[name]
    if ctx.customer_id is None:
        raise ToolError("NOT_APPLICABLE", "no customer in this conversation")
    try:
        order_id = UUID(str(args["order_id"]))
    except ValueError:
        order_id = None
    order = await commerce.get_order(ctx.business_id, order_id) if order_id else None
    if order is None or order.customer_id != ctx.customer_id:  # only the customer's own orders, never a guessed id
        raise ToolError(
            "NOT_FOUND",
            "no such order for this customer",
            guidance="Use an order_id from THIS CUSTOMER'S ORDERS or order_status. If they have no order with us, "
            "say so kindly; a complaint without an order goes to the owner (handoff).",
        )
    situation = await commerce.situation(ctx.business_id, order.order_id)
    summary = describe_order(order)
    if changes := args.get("changes"):
        summary += " → " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in changes.items())

    if why_not := cap.invariant(situation):
        await _trace(ctx, name, "not_possible", None)
        return {"outcome": "not_possible", "reason": why_not, "next": "Explain this kindly and say what is possible."}

    rule = await knowledge.playbook.match(ctx.business_id, name, situation, ctx.customer_id)
    effect: Effect = rule.effect if rule else cap.default(situation)
    basis = {"rule": rule.rule.says} if rule else {"rule": None, "default": f"{cap.risk} actions: {effect}"}

    if effect == "allow":
        try:
            result = await cap.execute(ctx.business_id, {**args, "reason": reason}, "customer")
        except commerce.CommerceError as exc:  # e.g. the new day is full, the date is too close
            return {"outcome": "not_possible", "reason": str(exc), "guidance": exc.guidance,
                    "next": "Explain kindly and offer what is possible."}  # fmt: skip
        await _fyi(ctx, cap, summary, reason, rule.rule.says if rule else None)
        await _trace(ctx, name, "done", rule)
        return {"outcome": "done", **result, **basis, "next": "Tell the customer it is done, briefly and warmly."}
    if effect == "deny":
        await _trace(ctx, name, "declined", rule)
        return {
            "outcome": "declined",
            "shop_rule": rule.rule.says if rule else None,
            "next": "Explain kindly that the shop does not do this (in your own words, from shop_rule), and offer "
            "what is possible instead.",
        }
    proposal = await _propose(ctx, s, cap, args, situation, summary, reason, note_for_owner)
    await _trace(ctx, name, "proposed", rule)
    return {
        "outcome": "asked_owner",
        "code": proposal.short_code,
        "next": "Tell the customer honestly that you have asked the shop and will update them as soon as they "
        "decide. Do not promise the outcome.",
    }


async def _propose(
    ctx: ToolContext,
    s: AsyncSession,
    cap: Capability,
    args: dict[str, Any],
    situation: dict[str, Any],
    summary: str,
    reason: str,
    note: str,
) -> approvals.Approval:
    assert ctx.conversation_id is not None
    for existing in await approvals.open_for(ctx.business_id, kind=PROPOSAL_KIND, conversation_id=ctx.conversation_id):
        same = existing.subject.get("capability") == cap.name and existing.subject.get("args") == args
        if same:  # asked again while the owner is still deciding: one proposal, not two
            return existing
    conv = await gateway.conversation_view(s, ctx.conversation_id)
    customer = conv.customer if conv else None
    name = (customer.display_name if customer else None) or "A customer"
    proposal = await approvals.create(
        s,
        ctx.business_id,
        PROPOSAL_KIND,
        {
            "capability": cap.name,
            "args": args,
            "reason": reason,
            "situation": situation,
            "conversation_id": str(ctx.conversation_id),
            "customer_id": str(ctx.customer_id),
            "customer_name": name,
            "source_turn_id": str(ctx.turn_id) if ctx.turn_id else None,
        },
        {"summary": summary, "brief": note, "label": cap.label},
        requested_by="agent:customer",
        ttl_min=PROPOSAL_TTL_MIN,
    )
    body = (
        f"🙋 *{name}* wants: {cap.label.lower()} {summary}\n"
        f"{note.strip()}\n\n"
        f"Reason they gave: “{reason.strip()}”\n"
        f"Reply `YES {proposal.short_code}` or `NO {proposal.short_code}`."
    )
    notifier = ports.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
    if notifier is not None:
        await notifier.notify(
            ctx.business_id,
            "proposal",
            body,
            dedupe_key=f"proposal:{proposal.id}",
            priority=PriorityClass.P1,
            buttons=[(f"apv:{proposal.id}:a", f"✅ Yes, {cap.verb}"[:20]), (f"apv:{proposal.id}:r", "❌ No")],
            ref={"approval_id": str(proposal.id)},
            about_conversation=ctx.conversation_id,
        )
    return proposal


async def _fyi(ctx: ToolContext, cap: Capability, summary: str, reason: str, rule_says: str | None) -> None:
    notifier = ports.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
    if notifier is None:
        return
    basis = f"your rule: “{rule_says}”" if rule_says else "a draft, nothing was promised yet"
    await notifier.notify(
        ctx.business_id,
        "action_done",
        f"✅ Done for the customer: {cap.label.lower()} {summary}.\nReason: “{reason.strip()}” · ({basis})",
        dedupe_key=f"done:{cap.name}:{ctx.idem(cap.name)}",
        priority=PriorityClass.P3,
        about_conversation=ctx.conversation_id,
    )


async def _trace(ctx: ToolContext, capability: str, outcome: str, rule: Any) -> None:
    await emit(
        "decision",
        PipelineEventStatus.OK,
        business_id=ctx.business_id,
        conversation_id=ctx.conversation_id,
        turn_id=ctx.turn_id,
        capability=capability,
        outcome=outcome,
        rule_id=str(rule.rule.id) if rule else None,
    )
