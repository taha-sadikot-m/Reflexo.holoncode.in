"""CEL variables from live rows (POC_06 §1.4): `order`, `customer`, `business` for guards, router and executors."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import get_db

CAKE_CATEGORY = "cakes"


async def order_variables(business_id: UUID, order: dict[str, Any]) -> dict[str, Any]:
    """From an `OrderView` dump (commerce): adds line categories and the cake count."""
    lines = order.get("lines") or []
    offering_ids = sorted({str(ln["offering_id"]) for ln in lines if ln.get("offering_id")})
    categories: dict[str, str] = {}
    if offering_ids:
        async with get_db().tenant_session(business_id) as s:
            rows = (
                await s.execute(
                    text("SELECT id, lower(category) AS c FROM offerings WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": offering_ids},
                )
            ).all()
        categories = {str(r.id): r.c or "" for r in rows}
    items = [
        {
            "name": ln.get("name"),
            "quantity": int(ln.get("quantity") or 1),
            "category": categories.get(str(ln.get("offering_id")), ""),
            "adjustment": bool(ln.get("adjustment")),
        }
        for ln in lines
    ]
    real = [i for i in items if not i["adjustment"]]  # "Eggless (…)" adjustment lines are not extra cakes
    return {
        "id": str(order.get("order_id") or order.get("id")),
        "number": order.get("order_number"),
        "items": real,
        "categories": sorted({i["category"] for i in real if i["category"]}),
        "cake_count": sum(i["quantity"] for i in real if i["category"] == CAKE_CATEGORY),
        "total_paise": int(order.get("total_paise") or 0),
        "delivery_date": order.get("delivery_date"),
        "delivery_slot": order.get("delivery_slot"),
        "payment_method": order.get("payment_method"),
        "payment_status": order.get("payment_status"),
        "status": order.get("status"),
        "customer_id": str(order.get("customer_id")) if order.get("customer_id") else None,
    }


async def customer_variables(business_id: UUID, customer_id: UUID | None) -> dict[str, Any]:
    if customer_id is None:
        return {"id": None, "name": "customer", "tags": [], "phone_masked": None}
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("SELECT id, display_name, phone_e164, tags FROM customers WHERE id = :c"), {"c": customer_id}
            )
        ).first()
    if row is None:
        return {"id": str(customer_id), "name": "customer", "tags": [], "phone_masked": None}
    phone = row.phone_e164 or ""
    return {
        "id": str(row.id),
        "name": row.display_name or "customer",
        "tags": list(row.tags or []),
        "phone_masked": f"{phone[:3]}•••••{phone[-4:]}" if len(phone) > 7 else None,
    }
