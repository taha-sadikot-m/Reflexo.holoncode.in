"""Orders (POC_05 §4.1–4.4): draft → details → confirm. Prices are copied from the catalogue rows server-side; the
model only ever passes ids, quantities and detail values, and gets displays back.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.knowledge.api as knowledge
from reflexo.commerce.models import CommerceError, DetailField, ItemRef, OrderLine, OrderView
from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.common.money import format_inr

EDITABLE = ("draft",)
TRUTHY = {"yes", "y", "haan", "ha", "han", "true", "1", "eggless", "ji"}
FALSY = {"no", "n", "nahi", "nahin", "false", "0", "with egg", "egg"}


# ---------------------------------------------------------------- detail schemas


async def applicable_fields(business_id: UUID, categories: set[str], offering_ids: set[str]) -> list[DetailField]:
    """Fields of every schema that applies to the order's items; specific schemas win over `all: true` ones."""
    specific: dict[str, DetailField] = {}
    general: dict[str, DetailField] = {}
    for schema in await knowledge.detail_schemas(business_id):
        scope = schema.get("applies_to") or {}
        target = None
        if (
            set(map(str.lower, scope.get("categories") or [])) & categories
            or set(map(str, scope.get("offering_ids") or [])) & offering_ids
        ):
            target = specific
        elif scope.get("all"):
            target = general
        if target is None:
            continue
        for f in schema.get("fields") or []:
            target.setdefault(
                f["key"],
                DetailField(
                    key=f["key"],
                    label=f.get("label", f["key"]),
                    type=f.get("type", "text"),
                    required=bool(f.get("required")),
                    options=list(f.get("options") or []),
                    hint=str(f.get("hint") or ""),
                ),
            )
    return list(specific.values()) + [f for k, f in general.items() if k not in specific]


DAY_PARTS = {
    "morning": {"morning", "subah", "सुबह"},
    "afternoon": {"afternoon", "noon", "dopahar", "dopehar", "दोपहर"},
    "evening": {"evening", "shaam", "sham", "शाम"},
    "night": {"night", "raat", "रात"},
}
_SLOT_START = re.compile(r"^\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?", re.IGNORECASE)


def _day_part(slot: str) -> str | None:
    """'4pm-8pm' -> 'evening' (by the slot's start hour)."""
    m = _SLOT_START.match(slot)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if (m.group(2) or "").lower() == "pm" else 0)
    return "morning" if hour < 12 else "afternoon" if hour < 16 else "evening" if hour < 20 else "night"


async def _validate_value(
    business_id: UUID, field: DetailField, raw: Any, notice_hours: int | None, subtotal: int
) -> tuple[Any, int | None]:
    """Normalise one detail value; returns (value, delivery_fee_paise if the field decided it). Raises CommerceError."""
    value = raw.strip() if isinstance(raw, str) else raw
    if field.type == "choice":
        options = {o.lower(): o for o in field.options}
        key = str(value).lower()
        if set(options) <= {"yes", "no"}:
            key = "yes" if key in TRUTHY or value is True else "no" if key in FALSY or value is False else key
        if key not in options:
            raise CommerceError(
                "VALIDATION_FAILED",
                f"{field.label}: '{value}' is not an option",
                guidance=f"Ask the customer to choose {field.label}: {', '.join(field.options)}.",
            )
        return options[key], None
    if field.type == "slot":
        options = {o.lower().replace(" ", ""): o for o in field.options}
        key = str(value).lower().replace(" ", "")
        if key not in options:  # "evening" / "shaam" -> the one slot in that part of the day, if unambiguous
            part = next((p for p, words in DAY_PARTS.items() if key in words), None)
            matches = [o for o in field.options if part and _day_part(o) == part]
            if len(matches) == 1:
                return matches[0], None
            raise CommerceError(
                "VALIDATION_FAILED",
                f"slot '{value}' is not available",
                guidance=f"Offer the delivery slots: {', '.join(field.options)}.",
            )
        return options[key], None
    if field.type == "date":
        try:
            d = date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise CommerceError(
                "VALIDATION_FAILED",
                f"'{value}' is not a date",
                guidance="Pass the date as YYYY-MM-DD (resolve 'kal'/'Saturday' using the current date).",
            ) from exc
        earliest = await knowledge.earliest_delivery_date(business_id, notice_hours=notice_hours)
        if d < earliest:
            raise CommerceError(
                "VALIDATION_FAILED",
                f"{d.isoformat()} is in the past" if d.year < earliest.year else f"{d.isoformat()} is too soon",
                guidance=f"The earliest possible delivery date is {earliest.strftime('%a %d %b %Y')} "
                f"({earliest.isoformat()}); check the year, explain the notice period and offer that date or later.",
                details={"earliest": earliest.isoformat()},
            )
        return d.isoformat(), None
    if field.type == "address":
        text_value = str(value)
        profile = await knowledge.get_profile(business_id)
        if text_value.lower() in ("pickup", "pick up", "self pickup", "khud le jaunga", "khud le jaungi"):
            if not profile.delivery.pickup_available:
                raise CommerceError(
                    "VALIDATION_FAILED", "pickup is not offered", guidance="Ask for a delivery address."
                )
            return "pickup", 0
        quote = await knowledge.delivery_quote(business_id, text_value)
        if not quote.serviceable:
            raise CommerceError(
                "NOT_SERVICEABLE",
                f"no delivery to '{text_value}'",
                guidance="Say we don't deliver there yet" + (" and offer pickup." if quote.pickup_available else "."),
            )
        fee = 0 if quote.free_above_paise and subtotal >= quote.free_above_paise else int(quote.fee_paise or 0)
        return text_value[:300], fee
    text_value = str(value)
    if not text_value:
        raise CommerceError("VALIDATION_FAILED", f"{field.label} is empty", guidance=f"Ask for {field.label}.")
    return text_value[:200], None


# ---------------------------------------------------------------- reading


async def _lines(s: AsyncSession, order_id: UUID) -> list[OrderLine]:
    rows = (
        await s.execute(
            text("""SELECT id, name_snapshot, quantity, unit_price_paise, line_total_paise, offering_id, variant_id,
                           details->>'adjustment' AS adj
                    FROM order_items WHERE order_id = :o ORDER BY (details->>'adjustment') NULLS FIRST, id"""),
            {"o": order_id},
        )
    ).all()
    return [
        OrderLine(
            line_id=r.id,
            name=r.name_snapshot,
            quantity=r.quantity,
            unit_price_paise=r.unit_price_paise,
            line_total_paise=r.line_total_paise,
            offering_id=r.offering_id,
            variant_id=r.variant_id,
            adjustment=r.adj,
        )
        for r in rows
    ]


async def _item_scope(
    s: AsyncSession, business_id: UUID, lines: list[OrderLine]
) -> tuple[set[str], set[str], int | None]:
    ids = list({ln.offering_id for ln in lines if ln.offering_id and not ln.adjustment})
    cards = await knowledge.cards_for(business_id, ids) if ids else []
    categories = {(c.category or "").lower() for c in cards if c.category}
    notice = [int(c.attributes["notice_hours"]) for c in cards if isinstance(c.attributes.get("notice_hours"), int)]
    return categories, {str(i) for i in ids}, (max(notice) if notice else None)


async def view(s: AsyncSession, order_id: UUID) -> OrderView:
    o = (await s.execute(text("SELECT * FROM orders WHERE id = :id"), {"id": order_id})).first()
    if o is None:
        raise CommerceError("NOT_FOUND", "order not found", guidance="Start a new order with order_start.")
    lines = await _lines(s, order_id)
    categories, offering_ids, _ = await _item_scope(s, o.business_id, lines)
    fields = await applicable_fields(o.business_id, categories, offering_ids)
    for f in fields:
        f.value = (o.details or {}).get(f.key)
    return OrderView(
        order_id=o.id,
        order_number=o.order_number,
        status=str(o.status),
        payment_method=str(o.payment_method) if o.payment_method else None,
        payment_status=str(o.payment_status),
        lines=lines,
        subtotal_paise=o.subtotal_paise,
        delivery_fee_paise=o.delivery_fee_paise,
        total_paise=o.total_paise,
        fields=fields,
        missing_details=list(o.missing_details or []),
        delivery_date=o.delivery_date.isoformat() if o.delivery_date else None,
        delivery_slot=o.delivery_slot,
        address=o.address,
        customer_id=o.customer_id,
        conversation_id=o.conversation_id,
    )


# ---------------------------------------------------------------- writing


async def _add_items(s: AsyncSession, business_id: UUID, order_id: UUID, items: list[ItemRef]) -> None:
    cards = {c.offering_id: c for c in await knowledge.cards_for(business_id, [i.offering_id for i in items])}
    for item in items:
        card = cards.get(item.offering_id)
        if card is None:
            raise CommerceError("NOT_FOUND", "product not available", guidance="Search the catalogue again.")
        priced = [v for v in card.variants if v.price_paise is not None]
        if card.price_on_request or not priced:
            raise CommerceError(
                "PRICE_ON_REQUEST",
                f"{card.name} has no fixed price",
                guidance="Use action=ask_owner so the owner can quote this item.",
            )
        if item.variant_id is None and len(priced) > 1:
            sizes = " or ".join(f"{v.name} ({v.price_display})" for v in priced)
            raise CommerceError("VALIDATION_FAILED", f"{card.name}: size needed", guidance=f"Ask which size: {sizes}.")
        variant = (
            priced[0] if item.variant_id is None else next((v for v in priced if v.variant_id == item.variant_id), None)
        )
        if variant is None:
            raise CommerceError("NOT_FOUND", "that size is not available", guidance="Offer the available sizes.")
        if not variant.in_stock:
            raise CommerceError(
                "OUT_OF_STOCK", f"{card.name} {variant.name} is out of stock", guidance="Offer another item."
            )
        name = f"{card.name} {variant.name}".strip()
        existing = (
            await s.execute(
                text("""SELECT id, quantity FROM order_items WHERE order_id = :o AND offering_id = :off
                          AND variant_id IS NOT DISTINCT FROM :v AND details->>'adjustment' IS NULL"""),
                {"o": order_id, "off": item.offering_id, "v": variant.variant_id},
            )
        ).first()
        price = int(variant.price_paise or 0)
        if existing is not None:  # same item again: set the quantity the customer asked for
            await s.execute(
                text("""UPDATE order_items SET quantity = :q, unit_price_paise = :p, line_total_paise = :t
                        WHERE id = :id"""),
                {"q": item.quantity, "p": price, "t": price * item.quantity, "id": existing.id},
            )
            continue
        await s.execute(
            text("""INSERT INTO order_items (id, business_id, order_id, offering_id, variant_id, name_snapshot,
                                             unit_price_paise, quantity, line_total_paise, details)
                    VALUES (:id, :b, :o, :off, :v, :n, :p, :q, :t, CAST(:d AS jsonb))"""),
            {
                "id": new_id(),
                "b": business_id,
                "o": order_id,
                "off": item.offering_id,
                "v": variant.variant_id,
                "n": name,
                "p": price,
                "q": item.quantity,
                "t": price * item.quantity,
                "d": json.dumps(item.details),
            },
        )


async def recompute(s: AsyncSession, business_id: UUID, order_id: UUID) -> None:
    """Server-side adjustments (eggless surcharge), totals, delivery fee and missing details."""
    o = (await s.execute(text("SELECT details FROM orders WHERE id = :id"), {"id": order_id})).one()
    details: dict[str, Any] = dict(o.details or {})
    await s.execute(
        text("DELETE FROM order_items WHERE order_id = :o AND details->>'adjustment' IS NOT NULL"), {"o": order_id}
    )
    lines = await _lines(s, order_id)
    order_eggless = str(details.get("eggless", "")).lower() in TRUTHY
    ids = [ln.offering_id for ln in lines if ln.offering_id]
    cards = {c.offering_id: c for c in await knowledge.cards_for(business_id, ids)} if ids else {}
    for ln in lines:
        card = cards.get(ln.offering_id) if ln.offering_id else None
        extra = card.attributes.get("eggless_extra_paise") if card else None
        if (
            card
            and (order_eggless)
            and card.attributes.get("eggless_available")
            and isinstance(extra, int)
            and extra > 0
        ):
            await s.execute(
                text("""INSERT INTO order_items (id, business_id, order_id, offering_id, name_snapshot,
                                                 unit_price_paise, quantity, line_total_paise, details)
                        VALUES (:id, :b, :o, :off, :n, :p, :q, :t, CAST(:d AS jsonb))"""),
                {
                    "id": new_id(),
                    "b": business_id,
                    "o": order_id,
                    "off": ln.offering_id,
                    "n": f"Eggless ({card.name})",
                    "p": extra,
                    "q": ln.quantity,
                    "t": extra * ln.quantity,
                    "d": json.dumps({"adjustment": "eggless"}),
                },
            )
    lines = await _lines(s, order_id)
    subtotal = sum(ln.line_total_paise for ln in lines)
    categories, offering_ids, _ = await _item_scope(s, business_id, lines)
    fields = await applicable_fields(business_id, categories, offering_ids)
    missing = [f.key for f in fields if f.required and details.get(f.key) in (None, "")]
    fee = 0
    address = details.get("address")
    if address and address != "pickup":
        quote = await knowledge.delivery_quote(business_id, str(address))
        if quote.serviceable:
            fee = 0 if quote.free_above_paise and subtotal >= quote.free_above_paise else int(quote.fee_paise or 0)
    await s.execute(
        text("""UPDATE orders SET subtotal_paise = :sub, delivery_fee_paise = :fee, total_paise = :total,
                                  missing_details = :missing, updated_at = now(),
                                  delivery_date = :dd, delivery_slot = :slot, address = :addr
                WHERE id = :id"""),
        {
            "sub": subtotal,
            "fee": fee,
            "total": subtotal + fee,
            "missing": missing,
            "id": order_id,
            "dd": date.fromisoformat(str(details["delivery_date"])) if details.get("delivery_date") else None,
            "slot": details.get("delivery_slot"),
            "addr": address,
        },
    )


async def _next_number(s: AsyncSession, business_id: UUID) -> str:
    value = (
        await s.execute(
            text("""UPDATE business_counters SET value = value + 1 WHERE business_id = :b AND name = 'order_number'
                    RETURNING value"""),
            {"b": business_id},
        )
    ).scalar_one_or_none()
    if value is None:  # first order of a business without a seeded counter
        await s.execute(
            text("INSERT INTO business_counters (business_id, name, value) VALUES (:b, 'order_number', 1001)"),
            {"b": business_id},
        )
        value = 1001
    settings = await knowledge.business_settings(business_id)
    # initials of the words ("Meera's Bakes" -> "MB"); a seeded `order_number_prefix` wins
    initials = "".join(next((c for c in w if c.isalpha()), "") for w in str(settings.get("name", "")).split())
    prefix = str(settings.get("order_number_prefix") or initials[:2].upper() or "OR")
    return f"{prefix}-{value}"


async def start_order(
    business_id: UUID,
    customer_id: UUID,
    conversation_id: UUID | None,
    items: list[ItemRef],
    *,
    channel: str | None = None,
    created_by: str = "ai",
) -> OrderView:
    """Create the customer's draft order, or add to the open draft (one open draft per customer)."""
    if not items:
        raise CommerceError("VALIDATION_FAILED", "no items", guidance="Ask what the customer wants to order.")
    async with get_db().tenant_session(business_id) as s:
        draft = (
            await s.execute(
                text("""SELECT id FROM orders WHERE customer_id = :c AND status = 'draft'
                        ORDER BY created_at DESC LIMIT 1 FOR UPDATE"""),
                {"c": customer_id},
            )
        ).scalar_one_or_none()
        if draft is None:
            draft = new_id()
            await s.execute(
                text("""INSERT INTO orders (id, business_id, order_number, customer_id, conversation_id, source_channel,
                                            created_by)
                        VALUES (:id, :b, :n, :c, :conv, CAST(:ch AS channel_type), :by)"""),
                {
                    "id": draft,
                    "b": business_id,
                    "n": await _next_number(s, business_id),
                    "c": customer_id,
                    "conv": conversation_id,
                    "ch": channel,
                    "by": created_by,
                },
            )
        await _add_items(s, business_id, draft, items)
        await recompute(s, business_id, draft)
        return await view(s, draft)


async def update_order(
    business_id: UUID,
    order_id: UUID,
    *,
    add_items: list[ItemRef] | None = None,
    remove_line_ids: list[UUID] | None = None,
    details: dict[str, Any] | None = None,
) -> OrderView:
    async with get_db().tenant_session(business_id) as s:
        status = (
            await s.execute(text("SELECT status FROM orders WHERE id = :id FOR UPDATE"), {"id": order_id})
        ).scalar_one_or_none()
        if status is None:
            raise CommerceError("NOT_FOUND", "order not found", guidance="Start a new order with order_start.")
        if str(status) not in EDITABLE:
            raise CommerceError(
                "NOT_EDITABLE",
                f"order is {status}",
                guidance="This order can no longer be changed here; offer to ask the owner.",
            )
        if add_items:
            await _add_items(s, business_id, order_id, add_items)
        if remove_line_ids:
            await s.execute(
                text("DELETE FROM order_items WHERE order_id = :o AND id = ANY(:ids)"),
                {"o": order_id, "ids": list(remove_line_ids)},
            )
        rejected: list[dict[str, Any]] = []
        if details:
            await recompute(s, business_id, order_id)  # subtotal for the free-delivery threshold
            current = await view(s, order_id)
            fields = {f.key: f for f in current.fields}
            merged = {f.key: f.value for f in current.fields if f.value not in (None, "")}
            _, _, notice = await _item_scope(s, business_id, current.lines)
            for key, raw in details.items():
                field = fields.get(key)
                if field is None:
                    merged[key] = raw if isinstance(raw, str | int | float | bool) else json.dumps(raw)
                    continue
                try:
                    merged[key], _fee = await _validate_value(business_id, field, raw, notice, current.subtotal_paise)
                except CommerceError as exc:  # keep the valid details of this call; report the rest
                    rejected.append(
                        {"key": key, "value": str(raw), "code": exc.code, "reason": str(exc), "guidance": exc.guidance}
                    )
            await s.execute(
                text("UPDATE orders SET details = CAST(:d AS jsonb) WHERE id = :id"),
                {"d": json.dumps(merged, ensure_ascii=False), "id": order_id},
            )
        await recompute(s, business_id, order_id)
        result = await view(s, order_id)
        if not result.lines:
            raise CommerceError(
                "VALIDATION_FAILED", "the order has no items", guidance="Ask what they'd like to order."
            )
        result.detail_errors = rejected
        return result


async def order_status(business_id: UUID, customer_id: UUID, order_number: str | None) -> OrderView | None:
    async with get_db().tenant_session(business_id) as s:
        oid = (
            await s.execute(
                text("""SELECT id FROM orders WHERE customer_id = :c AND (CAST(:n AS text) IS NULL OR order_number = :n)
                        ORDER BY created_at DESC LIMIT 1"""),
                {"c": customer_id, "n": order_number.upper() if order_number else None},
            )
        ).scalar_one_or_none()
        return await view(s, oid) if oid else None


async def get_order(business_id: UUID, order_id: UUID) -> OrderView | None:
    async with get_db().tenant_session(business_id) as s:
        exists = (await s.execute(text("SELECT 1 FROM orders WHERE id = :o"), {"o": order_id})).first()
        return await view(s, order_id) if exists else None


async def open_order(business_id: UUID, customer_id: UUID) -> OrderView | None:
    """The customer's order still in progress (draft or awaiting payment): what the next turn continues."""
    async with get_db().tenant_session(business_id) as s:
        oid = (
            await s.execute(
                text("""SELECT id FROM orders WHERE customer_id = :c AND status IN ('draft', 'awaiting_payment')
                        ORDER BY created_at DESC LIMIT 1"""),
                {"c": customer_id},
            )
        ).scalar_one_or_none()
        return await view(s, oid) if oid else None


def summary_lines(v: OrderView) -> list[str]:
    return [f"{ln.quantity} × {ln.name} — {format_inr(ln.line_total_paise)}" for ln in v.lines]
