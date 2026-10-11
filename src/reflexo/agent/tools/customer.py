"""Customer desk tools (POC_04 §3.2). Thin adapters over other modules' `api.py`; prices come from rows.

Order tools (`order_start`, `order_update`, `order_confirm`, `order_status`) are added with commerce (WP-09).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent.schemas import AgentOutput
from reflexo.agent.tools.base import ToolContext, ToolError, tool
from reflexo.agent.tools.orders import ORDER_TOOLS
from reflexo.common.money import format_inr

CUSTOMER = {"customer", "voice"}


def card_json(card: knowledge.CatalogCard) -> dict[str, Any]:
    out: dict[str, Any] = {
        "offering_id": str(card.offering_id),
        "name": card.name,
        "category": card.category,
        "in_stock": card.in_stock,
        "price_on_request": card.price_on_request,
        "variants": [
            {
                "variant_id": str(v.variant_id) if v.variant_id else None,
                "name": v.name or "standard",
                "price": v.price_display,
                "in_stock": v.in_stock,
            }
            for v in card.variants
        ],
    }
    if card.matched_variant_id:
        out["best_match_variant_id"] = str(card.matched_variant_id)
    extra = card.attributes.get("eggless_extra_paise")
    if card.attributes.get("eggless_available"):
        out["eggless"] = f"available (+{format_inr(extra)})" if isinstance(extra, int) and extra else "available"
    return out


# ---------------------------------------------------------------- catalogue


class CatalogSearchArgs(BaseModel):
    query: str = Field(description="What the customer wants, in their words (any language/spelling)")
    max_price_rupees: int | None = Field(default=None, description="Budget limit in rupees, if the customer gave one")
    category: str | None = Field(default=None, description="Category filter, e.g. cakes")
    in_stock_only: bool = False


@tool("catalog_search", CatalogSearchArgs, desks=CUSTOMER | {"owner"})
async def catalog_search(ctx: ToolContext, args: CatalogSearchArgs) -> dict[str, Any]:
    """Search the catalogue. Returns up to 5 products with live variant prices. Reference what you use as
    variant/offering records."""
    cards = await knowledge.search_catalog(
        ctx.business_id,
        args.query,
        max_price_paise=args.max_price_rupees * 100 if args.max_price_rupees else None,
        category=args.category,
        in_stock_only=args.in_stock_only,
    )
    return {"cards": [card_json(c) for c in cards], "count": len(cards)}


class CatalogGetArgs(BaseModel):
    offering_id: str = Field(description="offering_id from a catalog card")


@tool("catalog_get", CatalogGetArgs, desks=CUSTOMER)
async def catalog_get(ctx: ToolContext, args: CatalogGetArgs) -> dict[str, Any]:
    """Full details of one product: description, attributes (eggless, flavours, serves) and variant prices."""
    try:
        oid = UUID(args.offering_id)
    except ValueError as exc:
        raise ToolError(
            "INVALID_ARGS", "offering_id is not a valid id", guidance="Use an offering_id from a card."
        ) from exc
    card = await knowledge.get_card(ctx.business_id, oid)
    if card is None:
        raise ToolError("NOT_FOUND", "no such product", guidance="Search the catalogue again.")
    return {**card_json(card), "description": card.description, "attributes": card.attributes}


# ---------------------------------------------------------------- knowledge & delivery


class KnowledgeLookupArgs(BaseModel):
    query: str = Field(description="The customer's question about the business (policies, hours, payments...)")


@tool("knowledge_lookup", KnowledgeLookupArgs, desks=CUSTOMER | {"owner"})
async def knowledge_lookup(ctx: ToolContext, args: KnowledgeLookupArgs) -> dict[str, Any]:
    """Look up the owner's answers (Q&A) and business info (hours, delivery, policies, payments, contact).
    Reference hits as qa:<id> or profile:<section>."""
    hits = await knowledge.search_knowledge(ctx.business_id, args.query, customer_id=ctx.customer_id, k=4)
    return {
        "results": [
            {
                "ref": {"type": "qa", "id": str(h.entity_id)}
                if h.entity_type == "qa"
                else {"type": "profile", "id": h.title},
                "title": h.title,
                "text": h.text,
            }
            for h in hits
        ]
    }


class DeliveryQuoteArgs(BaseModel):
    area_or_pincode: str = Field(description="Customer's area name or 6-digit pincode")


@tool("delivery_quote", DeliveryQuoteArgs, desks=CUSTOMER)
async def delivery_quote(ctx: ToolContext, args: DeliveryQuoteArgs) -> dict[str, Any]:
    """Whether we deliver to an area/pincode, the fee, free-delivery threshold and notice needed.
    Reference the zone as delivery_zone:<zone_id>."""
    q = await knowledge.delivery_quote(ctx.business_id, args.area_or_pincode)
    out: dict[str, Any] = {
        "serviceable": q.serviceable,
        "notice_hours": q.notice_hours,
        "pickup_available": q.pickup_available,
        "pickup_address": q.pickup_address,
    }
    if q.serviceable:
        out |= {
            "zone_id": q.zone_id,
            "zone": q.zone_label,
            "fee": format_inr(q.fee_paise or 0),
            "free_above": format_inr(q.free_above_paise) if q.free_above_paise else None,
            "eta": q.eta,
        }
    return out


# ---------------------------------------------------------------- channel continuation


class NoArgs(BaseModel):
    pass


@tool("offer_whatsapp_continue", NoArgs, desks={"customer"}, side_effect=True)
async def offer_whatsapp_continue(ctx: ToolContext, args: NoArgs, s: AsyncSession) -> dict[str, Any]:
    """Only on Instagram/Messenger/Telegram: create a one-time link that continues this chat on WhatsApp
    (with history). Put the returned url in a text block."""
    if ctx.channel == "whatsapp":
        raise ToolError("NOT_APPLICABLE", "already on WhatsApp")
    if ctx.customer_id is None:
        raise ToolError("NOT_APPLICABLE", "no customer")
    code, url = await gateway.issue_link_code(s, ctx.business_id, ctx.customer_id, ctx.channel)
    return {"url": url, "code": code, "expires_in_minutes": 30}


# ---------------------------------------------------------------- terminal


@tool("reply", AgentOutput, desks={"customer"}, terminal=True)
async def reply(ctx: ToolContext, args: AgentOutput) -> dict[str, Any]:
    """Finish the turn. action=send with blocks and referenced_records for grounded answers; ask_owner with
    owner_question when the business has not told you; handoff for refunds, complaints, angry customers or
    requests for a human; no_reply when nothing needs saying."""
    return {"accepted": True}  # handled by the graph (gate → finalize); never executed as a normal tool


CUSTOMER_TOOLS = [
    "catalog_search",
    "catalog_get",
    "knowledge_lookup",
    "delivery_quote",
    "offer_whatsapp_continue",
    *ORDER_TOOLS,
    "reply",
]
