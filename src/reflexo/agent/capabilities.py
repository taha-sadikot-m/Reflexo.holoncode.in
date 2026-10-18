"""Capabilities and `decide()` (ADR-015, ADR-028; BETA_04 §7.1, §7.7).

A capability is something the employee can do (cancel an order, message a customer, ...). Every effectful path
asks `decide()` first: it builds the Cedar request (the principal, the business, the resource, its customer, the
situation and the history aggregates the capability declares) and evaluates the policy layers: platform
invariants, this customer's rules, the shop's rules, the risk defaults, the ceilings. The answer is `act`,
`decline`, `propose`, `not_possible` or `shadow`; nothing here knows about any one business.

`request()` is the customer desk's path for order capabilities: decide, then act, decline or propose to the
owner, who decides; the decision is carried out exactly as proposed (`ProposalService`).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.approvals.api as approvals
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
import reflexo.policy.api as policy
from reflexo.agent import messages
from reflexo.agent.tools.base import ToolContext, ToolError
from reflexo.common.config import get_settings
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.timeutil import utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)
Risk = Literal["routine", "commitment", "money"]
Requester = Literal["customer_turn", "owner_turn", "team_turn", "automation", "voice"]
PROPOSAL_KIND = "tool_call"  # approval_requests.kind for agent-proposed actions
PROPOSAL_TTL_MIN = 24 * 60
VOCABULARY = "capabilities.yaml"
PLACED = ("confirmed", "preparing", "out_for_delivery", "delivered")


class ActionRequest(BaseModel):
    capability: str  # 'order.cancel', 'message.send', ...
    business_id: UUID
    requested_by: Requester
    desk: str  # the Agent principal's desk: customer, owner, voice, automation
    customer_id: UUID | None = None
    member_id: UUID | None = None  # set when a person decides: principal Member
    resource: policy.EntityRef  # Order | Customer | Business | ...
    context: dict[str, Any] = Field(default_factory=dict)  # facts the caller already has (channel, topic, ...)
    owner_approved: bool = False  # true only when executing a decided proposal
    conversation_id: UUID | None = None  # where the request came from (customer mode, X-ray)


Decision = policy.Decision


@dataclass(frozen=True)
class Capability:
    name: str
    risk: Risk
    resource: str  # the Cedar resource type
    execute: Callable[[UUID, dict[str, Any], str], Awaitable[dict[str, Any]]] | None = None  # (business, args, by)
    aggregates: tuple[str, ...] = ()  # history aggregates its context declares (BETA_04 §7.7)

    @property
    def label(self) -> str:
        """How the owner reads it: "Cancelling an order"."""
        return str(_vocabulary()[self.name]["label"]["en"])

    def label_in(self, lang: str) -> str:
        labels = _vocabulary()[self.name]["label"]
        return str(labels.get(lang) or labels["en"])

    @property
    def verb(self) -> str:
        """The owner's approve button: "cancel"."""
        return str(_vocabulary()[self.name]["verb"])


@lru_cache(maxsize=1)
def _vocabulary() -> dict[str, Any]:
    path = Path(get_settings().policy_dir) / VOCABULARY
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


# ---------------------------------------------------------------- executors


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


# Every action of the Cedar schema (CHK-POL-8). Executors exist for the capabilities built so far; the others are
# decided here by the work packages that build them (BETA_09).
CAPABILITIES: dict[str, Capability] = {
    c.name: c
    for c in (
        Capability("order.cancel", "commitment", "Order", _cancel, ("cancellations_this_week",)),
        Capability("order.change", "commitment", "Order", _change),
        Capability("refund.request", "money", "Order", _refund, ("refunds_today_rupees",)),
        Capability("message.send", "routine", "Customer", None, ("automated_messages_to_customer_today",)),
        Capability("reply.answer", "routine", "Customer"),
        Capability("broadcast.send", "commitment", "Business"),
        Capability("call.place", "commitment", "Customer"),
        Capability("customer.update", "routine", "Customer"),
        Capability("webhook.call", "commitment", "Business"),
        Capability("order.confirm_by_customer", "routine", "Order"),
        Capability("order.tag", "routine", "Order"),
        Capability("return.request", "commitment", "Order"),
        Capability("payment.request", "routine", "Order"),
        Capability("payment.confirm_manual", "money", "Order"),
        Capability("stock.check", "routine", "Product"),
        Capability("shipment.track", "routine", "Shipment"),
        Capability("slot.find", "routine", "Business"),
        Capability("booking.create", "commitment", "Booking"),
        Capability("comment.hide", "commitment", "Comment"),
    )
}


def describe_order(o: commerce.OrderView) -> str:
    items = ", ".join(f"{ln.quantity} × {ln.name}" if ln.quantity > 1 else ln.name for ln in o.lines)
    when = " ".join(x for x in (o.delivery_date or "", o.delivery_slot or "") if x)
    pay = {"cod": "COD", "razorpay_link": "online"}.get(o.payment_method or "", "")
    facts = [f"{format_inr(o.total_paise)}{' ' + pay if pay else ''}", when, o.status.replace("_", " ")]
    return f"{o.order_number} ({items}; " + ", ".join(f for f in facts if f) + ")"


# ---------------------------------------------------------------- decide()


def _whole(value: Any) -> int | None:
    return None if value is None else math.floor(float(value))


_CONTEXT_SQL = text("""
    SELECT (SELECT cc.customer_mode::text FROM conversations cv
              JOIN channel_connections cc ON cc.id = cv.channel_connection_id
             WHERE cv.customer_id = CAST(:c AS uuid) AND cv.participant_role = 'customer'
             ORDER BY (cv.id = CAST(:conv AS uuid)) DESC NULLS LAST, cv.last_inbound_at DESC NULLS LAST
             LIMIT 1) AS customer_mode,
           (SELECT count(*) FROM orders WHERE customer_id = CAST(:c AS uuid) AND id IS DISTINCT FROM CAST(:o AS uuid)
               AND status::text = ANY(:placed)) AS orders_count,
           CASE WHEN :w_refunds THEN
             (SELECT coalesce(sum((payload->>'total_paise')::bigint), 0) FROM domain_events
               WHERE type = 'payment.refunded' AND occurred_at >= :day_start) END AS refunds_paise,
           CASE WHEN :w_cancels THEN
             (SELECT count(*) FROM orders WHERE cancelled_at >= now() - interval '7 days') END AS cancellations,
           -- automation sends are keyed auto:... by the automation runtime (messages.origin arrives with WP-03)
           CASE WHEN :w_auto THEN
             (SELECT count(*) FROM outbound_messages o JOIN conversations cv ON cv.id = o.conversation_id
               WHERE cv.customer_id = CAST(:c AS uuid) AND o.idempotency_key LIKE 'auto:%' AND o.part_index = 0
                 AND o.created_at >= :day_start) END AS automated
""")


@dataclass
class _Built:
    request: policy.PolicyRequest
    versions: dict[str, int | None]
    situation: dict[str, Any]


async def _member_role(business_id: UUID, member_id: UUID) -> tuple[str, list[str]]:
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(text("SELECT role::text AS role, scopes FROM team_members WHERE id = :m"), {"m": member_id})
        ).first()
    return (str(row.role), list(row.scopes or [])) if row else ("staff", [])


async def _build(req: ActionRequest, cap: Capability) -> _Built:
    bid = req.business_id
    settings = await knowledge.business_settings(bid)
    tz = ZoneInfo(str(settings.get("timezone") or "Asia/Kolkata"))
    day_start = datetime.combine(utcnow().astimezone(tz).date(), time(0), tzinfo=tz)

    entities: list[dict[str, Any]] = [policy.business_entity(bid)[1]]
    if req.member_id:
        role, scopes = await _member_role(bid, req.member_id)
        principal, ent = policy.member_entity(bid, req.member_id, role, scopes)
    else:
        principal, ent = policy.agent_entity(bid, req.desk)
    entities.append(ent)

    situation: dict[str, Any] = {}
    order_id: UUID | None = None
    customer_id = req.customer_id
    if cap.resource == "Order":
        order_id = UUID(req.resource.id)
        situation = await commerce.situation(bid, order_id)
        order = await commerce.get_order(bid, order_id)
        if order is None:
            raise commerce.CommerceError("NOT_FOUND", "order not found")
        customer_id = order.customer_id

    async with get_db().tenant_session(bid) as s:
        row = (
            await s.execute(
                _CONTEXT_SQL,
                {
                    "c": customer_id,
                    "conv": req.conversation_id,
                    "o": order_id,
                    "placed": list(PLACED),
                    "day_start": day_start,
                    "w_refunds": "refunds_today_rupees" in cap.aggregates,
                    "w_cancels": "cancellations_this_week" in cap.aggregates,
                    "w_auto": "automated_messages_to_customer_today" in cap.aggregates,
                },
            )
        ).one()

    context: dict[str, Any] = {
        **req.context,
        "requested_by": req.requested_by,
        "owner_approved": req.owner_approved,
    }
    if row.customer_mode and "customer_mode" not in context:
        context["customer_mode"] = row.customer_mode
    customer_ref = None
    if customer_id is not None:
        orders_count = situation.get("customer.orders_count", row.orders_count)
        customer_ref, cent = policy.customer_entity(bid, customer_id, int(orders_count or 0))
        entities.append(cent)

    if cap.resource == "Order":
        assert order_id is not None and customer_ref is not None
        resource, oent = policy.order_entity(
            bid,
            order_id,
            customer_ref,
            status=str(situation["order.status"]),
            payment_method=situation.get("order.payment_method"),
            payment_status=situation.get("order.payment_status"),
            total_rupees=_whole(situation.get("order.total_rupees")) or 0,
            categories=list(situation.get("order.categories") or []),
        )
        entities.append(oent)
        for field, key in (("order.hours_until_delivery", "hours_until_delivery"),
                           ("order.hours_since_placed", "hours_since_placed")):  # fmt: skip
            if situation.get(field) is not None:
                context[key] = _whole(situation[field])
        if cap.risk == "money":
            context["amount_rupees"] = _whole(situation.get("order.total_rupees")) or 0
    elif cap.resource == "Customer":
        if customer_ref is None:
            raise ToolError("NOT_APPLICABLE", "no customer for this action")
        resource = customer_ref
    elif cap.resource == "Business":
        resource = policy.EntityRef(type="Business", id=str(bid))
    else:
        raise ToolError("NOT_APPLICABLE", f"{cap.name} is not available yet")

    # limits over history count the requested action as done (BETA_04 §7.7)
    if row.refunds_paise is not None:
        before = math.floor(int(row.refunds_paise) / 100)
        context["refunds_today_rupees"] = before + int(context.get("amount_rupees", 0))
    if row.cancellations is not None:
        context["cancellations_this_week"] = int(row.cancellations) + 1
    if row.automated is not None:
        context["automated_messages_to_customer_today"] = int(row.automated) + 1

    preq = policy.PolicyRequest(
        principal=principal,
        action=cap.name,
        resource=resource,
        context=context,
        entities=entities,
        owner_approved=req.owner_approved,
    )
    return _Built(preq, await knowledge.policysets.current(bid), situation)


@lru_cache(maxsize=512)
def _cached_set(business_id: UUID, layer: str, version: int, cedar_text: str) -> policy.LoadedSet:
    return policy.load(layer, cedar_text)


_TEXTS: dict[tuple[UUID, str, int], str] = {}


async def _layer(business_id: UUID, layer: str, version: int | None) -> policy.LoadedSet:
    """Parsed handles per (business, layer, version): a version never changes, so it is parsed once."""
    if version is None:
        return policy.load(layer, "")
    key = (business_id, layer, version)
    if key not in _TEXTS:
        _TEXTS[key], _ = await knowledge.policysets.text_of(business_id, layer, version)
    return _cached_set(business_id, layer, version, _TEXTS[key])


async def decide(req: ActionRequest) -> Decision:
    """The one gate for every effectful action (BETA_04 §7.1). Writes the X-ray `decision` record."""
    cap = CAPABILITIES.get(req.capability)
    if cap is None:
        decision = Decision(effect="not_possible", layer="platform", reason=f"unknown capability {req.capability}")
    else:
        built = await _build(req, cap)
        sets = policy.LayerSets(
            customer=await _layer(req.business_id, "customer", built.versions["customer"]),
            shop=await _layer(req.business_id, "shop", built.versions["shop"]),
        )
        rules: list[policy.Rule] = []
        if get_settings().policy_engine == "reference":
            rules = list(await knowledge.playbook.rules(req.business_id))
        decision = policy.evaluate(built.request, sets, rules)
    await emit(
        "decision",
        PipelineEventStatus.OK,
        business_id=req.business_id,
        conversation_id=req.conversation_id,
        capability=req.capability,
        effect=decision.effect,
        layer=decision.layer,
        policy_ids=decision.policy_ids,
        requested_by=req.requested_by,
        owner_approved=req.owner_approved,
    )
    return decision


def _requester(ctx: ToolContext) -> Requester:
    return "voice" if ctx.desk == "voice" else "customer_turn"


# ---------------------------------------------------------------- the customer desk's path


async def request(
    ctx: ToolContext,
    s: AsyncSession,
    name: str,
    args: dict[str, Any],
    *,
    reason: str,
    note_for_owner: str,
) -> dict[str, Any]:
    """decide() → act, decline or propose. Returns the tool result the model turns into words for the customer;
    it never decides the outcome itself."""
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
    decision = await decide(
        ActionRequest(
            capability=name,
            business_id=ctx.business_id,
            requested_by=_requester(ctx),
            desk=ctx.desk,
            customer_id=ctx.customer_id,
            resource=policy.EntityRef(type="Order", id=str(order.order_id)),
            conversation_id=ctx.conversation_id,
        )
    )
    summary = describe_order(order)
    change = None
    if changes := args.get("changes"):
        change = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in changes.items())
        summary += f" → {change}"

    if decision.effect == "not_possible":
        return {"outcome": "not_possible", "reason": decision.reason,
                "next": "Explain this kindly and say what is possible."}  # fmt: skip
    ruled = decision.layer in ("customer", "shop")
    basis = {"rule": decision.says} if ruled else {"rule": None, "default": f"{cap.risk} actions: {decision.effect}"}
    if decision.effect == "act":
        try:
            result = await cap.execute(ctx.business_id, {**args, "reason": reason}, "customer")  # type: ignore[misc]
        except commerce.CommerceError as exc:  # e.g. the new day is full, the date is too close
            return {"outcome": "not_possible", "reason": str(exc), "guidance": exc.guidance,
                    "next": "Explain kindly and offer what is possible."}  # fmt: skip
        await _fyi(ctx, cap, order, summary, change, reason, decision.says if ruled else None)
        return {"outcome": "done", **result, **basis, "next": "Tell the customer it is done, briefly and warmly."}
    if decision.effect == "decline":
        return {
            "outcome": "declined",
            "shop_rule": decision.says,
            "next": "Explain kindly that the shop does not do this (in your own words, from shop_rule), and offer "
            "what is possible instead.",
        }
    # propose, or shadow (WP-19 adds shadow drafts; until then the owner decides, which is the safe side)
    situation = await commerce.situation(ctx.business_id, order.order_id)
    proposal = await _propose(ctx, s, cap, args, situation, order, summary, change, reason, note_for_owner)
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
    order: commerce.OrderView,
    summary: str,
    change: str | None,
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
    name = (customer.display_name if customer else None) or ""
    media = await gateway.recent_customer_media(s, ctx.conversation_id)  # e.g. the photo of a damaged cake
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
            "customer_name": name or None,
            "source_turn_id": str(ctx.turn_id) if ctx.turn_id else None,
        },
        {"summary": summary, "brief": note, "label": cap.label},
        requested_by="agent:customer",
        ttl_min=PROPOSAL_TTL_MIN,
    )
    await messages.notify_owner(
        "owner.proposal",
        business_id=ctx.business_id,
        facts={
            "customer.name": name or None,
            "action": cap.label,
            "order.number": order.order_number,
            "order.total": messages.Amount(paise=order.total_paise),
            "change": change,
            "reason": reason.strip(),
            "code": proposal.short_code,
            "note": note.strip() or None,  # the assistant's note, as it wrote it
        },
        context={"order": summary, "approve_verb": cap.verb},
        kind="proposal",
        dedupe_key=f"proposal:{proposal.id}",
        priority=PriorityClass.P1,
        buttons={"approve": f"apv:{proposal.id}:a", "reject": f"apv:{proposal.id}:r"},
        ref={"approval_id": str(proposal.id)},
        about_conversation=ctx.conversation_id,
        media=media,
    )
    return proposal


async def _fyi(
    ctx: ToolContext,
    cap: Capability,
    order: commerce.OrderView,
    summary: str,
    change: str | None,
    reason: str,
    rule_says: str | None,
) -> None:
    await messages.notify_owner(
        "owner.action_done",
        business_id=ctx.business_id,
        facts={
            "action": cap.label,
            "order.number": order.order_number,
            "order.total": messages.Amount(paise=order.total_paise),
            "change": change,
            "reason": reason.strip(),
            "rule": rule_says,
        },
        context={
            "order": summary,
            "basis": "the owner's rule" if rule_says else "a draft order: nothing was promised yet",
        },
        kind="action_done",
        dedupe_key=f"done:{cap.name}:{ctx.idem(cap.name)}",
        priority=PriorityClass.P3,
        about_conversation=ctx.conversation_id,
    )
