"""BlockHydrator implementation (POC_03 §8.3): product cards from live rows; payment/order views via commerce."""

from __future__ import annotations

from uuid import UUID

from reflexo.common.blocks import OrderSummaryView, PaymentLinkView, ProductView
from reflexo.common.contracts import CommerceViewsPort, MediaUrlPort
from reflexo.common.db import get_db
from reflexo.common.ports import Registry
from reflexo.knowledge.search import get_cards


class KnowledgeHydrator:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    async def product(self, business_id: UUID, offering_id: UUID, variant_id: UUID | None) -> ProductView:
        async with get_db().tenant_session(business_id) as s:
            card = (await get_cards(s, [offering_id])).get(offering_id)
        if card is None:
            raise LookupError(f"offering {offering_id} is not available")
        if variant_id is not None:
            chosen = [v for v in card.variants if v.variant_id == variant_id]
            if not chosen:
                raise LookupError(f"variant {variant_id} is not available")
            card = card.model_copy(update={"variants": chosen})
        signer = self.registry.optional(MediaUrlPort)  # type: ignore[type-abstract]
        image_url = signer.url_for(card.image_media_id) if signer and card.image_media_id else None
        return card.product_view(image_url)

    def _commerce(self) -> CommerceViewsPort:
        views = self.registry.optional(CommerceViewsPort)  # type: ignore[type-abstract]
        if views is None:
            raise LookupError("commerce views are not available (commerce module not registered)")
        return views

    async def payment_link(self, business_id: UUID, payment_link_id: UUID) -> PaymentLinkView:
        view: PaymentLinkView = await self._commerce().payment_link(business_id, payment_link_id)
        return view

    async def order_summary(self, business_id: UUID, order_id: UUID) -> OrderSummaryView:
        view: OrderSummaryView = await self._commerce().order_summary(business_id, order_id)
        return view
