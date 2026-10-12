"""Action tools (ADR-015): the customer desk *requests* a capability; `capabilities.request` decides (invariants →
the owner's playbook → default authority) and either does it, declines or proposes it to the owner. The model
never decides the outcome — it reports what the tool says."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.agent import capabilities
from reflexo.agent.tools.base import ToolContext, tool

ACTION_DESKS = {"customer", "voice"}
REASON_DESC = "Why the customer wants this, in their words"
NOTE_DESC = (
    "For the shop owner, as a colleague would write it (2–3 short lines, their language): who, what they want, "
    "the relevant facts and what you suggest — e.g. 'Taha wants to cancel MB-1028 (500 g Truffle, Thu 15 Oct), "
    "placed 10 min ago, not prepared yet. I suggest cancelling.' Used only if the owner has to decide."
)


class OrderCancelArgs(BaseModel):
    order_id: str = Field(description="The order to cancel (from OPEN ORDER, order_status or an earlier tool)")
    reason: str = Field(description=REASON_DESC, max_length=500)
    note_for_owner: str = Field(description=NOTE_DESC, max_length=600)


@tool("order_cancel", OrderCancelArgs, desks=ACTION_DESKS, side_effect=True)
async def order_cancel(ctx: ToolContext, args: OrderCancelArgs, s: AsyncSession) -> dict[str, Any]:
    """The customer wants to cancel an order. Returns outcome: done | declined (the shop's rule says no) |
    asked_owner (the shop decides; tell the customer you've asked) | not_possible (with the reason)."""
    return await capabilities.request(
        ctx,
        s,
        "order.cancel",
        {"order_id": args.order_id},
        reason=args.reason,
        note_for_owner=args.note_for_owner,
    )


class OrderChangeArgs(BaseModel):
    order_id: str = Field(description="The placed order to change (from OPEN ORDER, order_status or an earlier tool)")
    changes: dict[str, str] = Field(
        min_length=1,
        description="Detail keys → new values, e.g. {'delivery_date': 'YYYY-MM-DD' (use the Dates line), "
        "'delivery_slot': '4pm-8pm', 'address': '...', 'message_on_cake': '...'}",
    )
    reason: str = Field(description=REASON_DESC, max_length=500)
    note_for_owner: str = Field(description=NOTE_DESC, max_length=600)


@tool("order_change", OrderChangeArgs, desks=ACTION_DESKS, side_effect=True)
async def order_change(ctx: ToolContext, args: OrderChangeArgs, s: AsyncSession) -> dict[str, Any]:
    """The customer wants to change a placed order (date, time slot, address, message on cake…). For a draft use
    order_update instead. Returns outcome: done | declined | asked_owner | not_possible (with the reason)."""
    return await capabilities.request(
        ctx,
        s,
        "order.change",
        {"order_id": args.order_id, "changes": args.changes},
        reason=args.reason,
        note_for_owner=args.note_for_owner,
    )


class RefundRequestArgs(BaseModel):
    order_id: str = Field(description="The paid order the customer wants their money back for")
    reason: str = Field(description=REASON_DESC, max_length=500)
    note_for_owner: str = Field(description=NOTE_DESC, max_length=600)


@tool("refund_request", RefundRequestArgs, desks=ACTION_DESKS, side_effect=True)
async def refund_request(ctx: ToolContext, args: RefundRequestArgs, s: AsyncSession) -> dict[str, Any]:
    """The customer wants their money back for a paid order (e.g. after a problem with it, or a cancelled paid
    order). Returns outcome: done | declined | asked_owner | not_possible (with the reason)."""
    return await capabilities.request(
        ctx, s, "refund.request", {"order_id": args.order_id}, reason=args.reason, note_for_owner=args.note_for_owner
    )


ACTION_TOOLS = ["order_cancel", "order_change", "refund_request"]
