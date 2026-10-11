"""Order tools for the customer (and voice) desk (POC_04 §3.2): thin adapters over `commerce.api`.

The model passes ids, quantities and detail values; every amount is computed server-side and returned as a display.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.commerce.api as commerce
from reflexo.agent.tools.base import ToolContext, ToolError, tool

ORDER_DESKS = {"customer", "voice"}


class ItemArg(BaseModel):
    offering_id: str = Field(description="offering_id from a catalog card")
    variant_id: str | None = Field(default=None, description="variant_id of the chosen size (required if several)")
    quantity: int = Field(default=1, ge=1, le=50)


def _items(items: list[ItemArg]) -> list[commerce.ItemRef]:
    try:
        return [
            commerce.ItemRef(
                offering_id=UUID(i.offering_id),
                variant_id=UUID(i.variant_id) if i.variant_id else None,
                quantity=i.quantity,
            )
            for i in items
        ]
    except ValueError as exc:
        raise ToolError(
            "INVALID_ARGS", "bad id", guidance="Use offering_id/variant_id exactly as in the cards."
        ) from exc


def _uuid(value: str, name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ToolError("INVALID_ARGS", f"bad {name}", guidance=f"Use the {name} returned by the order tools.") from exc


def _guide(view: commerce.OrderView) -> dict[str, Any]:
    out = view.tool_view()
    out["reference"] = {"type": "order", "id": str(view.order_id)}
    out["next"] = (
        "Ask for the missing details (one or two at a time), then call order_update."
        if view.missing_details
        else "Read the order back (items, date, address, total) and ask how they want to pay, then order_confirm."
    )
    return out


async def _call(coro: Any) -> Any:
    try:
        return await coro
    except commerce.CommerceError as exc:
        raise ToolError(exc.code, str(exc), guidance=exc.guidance) from exc


class OrderStartArgs(BaseModel):
    items: list[ItemArg] = Field(min_length=1, description="What the customer wants to order")


@tool("order_start", OrderStartArgs, desks=ORDER_DESKS, side_effect=True)
async def order_start(ctx: ToolContext, args: OrderStartArgs, s: AsyncSession) -> dict[str, Any]:
    """Start the customer's order (or add to their open draft). Returns lines, totals and the details still needed.
    Reference the order as order:<order_id>."""
    if ctx.customer_id is None:
        raise ToolError("NOT_APPLICABLE", "no customer in this conversation")
    view = await _call(
        commerce.start_order(
            ctx.business_id,
            ctx.customer_id,
            ctx.conversation_id,
            _items(args.items),
            channel=ctx.channel if ctx.channel != "voice" else "whatsapp",
            created_by="voice" if ctx.desk == "voice" else "ai",
        )
    )
    return _guide(view)


class OrderUpdateArgs(BaseModel):
    order_id: str
    add_items: list[ItemArg] = Field(default_factory=list)
    remove_line_ids: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Detail values by key, e.g. {'delivery_date': 'YYYY-MM-DD', 'delivery_slot': '4pm-8pm', "
        "'address': '...', 'eggless': 'yes', 'message_on_cake': '...'}",
    )


@tool("order_update", OrderUpdateArgs, desks=ORDER_DESKS, side_effect=True)
async def order_update(ctx: ToolContext, args: OrderUpdateArgs, s: AsyncSession) -> dict[str, Any]:
    """Add/remove items or set order details. Dates are validated against the notice period, slots against the
    options and addresses against delivery zones (which also sets the delivery fee)."""
    view = await _call(
        commerce.update_order(
            ctx.business_id,
            _uuid(args.order_id, "order_id"),
            add_items=_items(args.add_items) if args.add_items else None,
            remove_line_ids=[_uuid(x, "line_id") for x in args.remove_line_ids] or None,
            details=args.details or None,
        )
    )
    return _guide(view)


class OrderConfirmArgs(BaseModel):
    order_id: str
    payment_method: Literal["razorpay_link", "cod"] = Field(
        description="razorpay_link (UPI/card link) unless the customer asks for cash on delivery"
    )


@tool("order_confirm", OrderConfirmArgs, desks=ORDER_DESKS, side_effect=True)
async def order_confirm(ctx: ToolContext, args: OrderConfirmArgs, s: AsyncSession) -> dict[str, Any]:
    """Confirm the order after the customer agreed to the read-back. Creates the payment link, or confirms COD if
    the business allows it for this total. May decline (capacity full, COD not allowed) with alternatives."""
    result = await _call(
        commerce.confirm_order(
            ctx.business_id, _uuid(args.order_id, "order_id"), args.payment_method, idem=ctx.idem("confirm")
        )
    )
    out: dict[str, Any] = {"status": result.status, "order": _guide(result.order)}
    if result.status == "awaiting_payment" and result.payment_link_id:
        out["reference"] = {"type": "payment_link", "id": str(result.payment_link_id)}
        out["next"] = (
            "Send a payment_link block with this payment_link_id (never type the URL) and say the order is "
            "confirmed as soon as the payment goes through."
        )
    elif result.status == "confirmed":
        out["next"] = "Send an order_summary block for this order_id and thank the customer."
    else:
        out["decline"] = {
            "code": result.decline_code,
            "reason": result.decline_reason,
            "alternatives": result.alternatives,
        }
        out["next"] = "Explain the reason kindly and offer the alternatives (e.g. pay by link, or another date)."
    return out


class OrderStatusArgs(BaseModel):
    order_number: str | None = Field(default=None, description="e.g. MB-1042; omit for the latest order")


@tool("order_status", OrderStatusArgs, desks=ORDER_DESKS)
async def order_status(ctx: ToolContext, args: OrderStatusArgs) -> dict[str, Any]:
    """The customer's latest (or named) order: status, payment and delivery."""
    if ctx.customer_id is None:
        raise ToolError("NOT_APPLICABLE", "no customer in this conversation")
    view = await commerce.order_status(ctx.business_id, ctx.customer_id, args.order_number)
    if view is None:
        return {"found": False}
    return {"found": True, **_guide(view)}


ORDER_TOOLS = ["order_start", "order_update", "order_confirm", "order_status"]
