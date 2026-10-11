"""Public interface of the `knowledge` module. Other modules import only from here (POC_01 §4).

Business profile, order detail schemas, offerings, Q&A, change sets, search, imports. Spec: POC_05 §1-3.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from reflexo.common.contracts import BlockHydrator, EmbedderPort, JobEnqueuer, JobHandlerRegistry, JobRow
from reflexo.common.db import get_db
from reflexo.common.ports import Registry
from reflexo.knowledge import changesets, indexer, profile, search
from reflexo.knowledge.changesets import add, apply, apply_now, begin, preview, revert
from reflexo.knowledge.hydrator import KnowledgeHydrator
from reflexo.knowledge.models import (
    BusinessProfile,
    CatalogCard,
    ChangeConflict,
    ChangeSet,
    ChangeSetPreview,
    DeliveryQuote,
    KnowledgeHit,
    OfferingAttributes,
    RenderedProfile,
    SpeculativeResult,
    VariantInfo,
)
from reflexo.knowledge.query import ParsedQuery, canonical_size, parse_query

__all__ = [
    "BusinessProfile",
    "CatalogCard",
    "ChangeConflict",
    "ChangeSet",
    "ChangeSetPreview",
    "DeliveryQuote",
    "KnowledgeHit",
    "OfferingAttributes",
    "ParsedQuery",
    "RenderedProfile",
    "SpeculativeResult",
    "VariantInfo",
    "add",
    "apply",
    "apply_now",
    "begin",
    "canonical_size",
    "delivery_quote",
    "earliest_delivery_date",
    "get_card",
    "is_open",
    "match_image_description",
    "next_open",
    "parse_query",
    "preview",
    "profile_block",
    "register",
    "reindex_business",
    "revert",
    "search_catalog",
    "search_knowledge",
    "speculative",
]

_registry: Registry | None = None


def _embedder() -> EmbedderPort | None:
    return _registry.optional(EmbedderPort) if _registry else None  # type: ignore[type-abstract]


def register(registry: Registry) -> None:
    global _registry
    _registry = registry
    changesets.bind_registry(registry)
    registry.provide(BlockHydrator, KnowledgeHydrator(registry))  # type: ignore[type-abstract]
    jobs = registry.optional(JobHandlerRegistry)  # type: ignore[type-abstract]
    if jobs is not None:

        async def reindex(job: JobRow) -> None:
            await indexer.embed_pending(UUID(job.payload["business_id"]), _embedder())

        jobs.register("knowledge.reindex", reindex)


# ---------------------------------------------------------------- search


async def search_catalog(
    business_id: UUID,
    query: str,
    *,
    max_price_paise: int | None = None,
    category: str | None = None,
    in_stock_only: bool = False,
    k: int | None = None,
) -> list[CatalogCard]:
    return await search.search_catalog(
        business_id,
        query,
        max_price_paise=max_price_paise,
        category=category,
        in_stock_only=in_stock_only,
        k=k,
        embedder=_embedder(),
    )


async def search_knowledge(
    business_id: UUID, query: str, *, customer_id: UUID | None = None, k: int = 3
) -> list[KnowledgeHit]:
    return await search.search_knowledge(business_id, query, customer_id=customer_id, k=k, embedder=_embedder())


async def speculative(
    business_id: UUID, text: str, *, customer_id: UUID | None = None, budget_ms: int | None = None
) -> SpeculativeResult:
    return await search.speculative(
        business_id, text, customer_id=customer_id, budget_ms=budget_ms, embedder=_embedder()
    )


async def match_image_description(
    business_id: UUID, description: str, visible_text: str | None = None
) -> list[CatalogCard]:
    return await search.match_image_description(business_id, description, visible_text, embedder=_embedder())


async def get_card(business_id: UUID, offering_id: UUID) -> CatalogCard | None:
    async with get_db().tenant_session(business_id) as s:
        return (await search.get_cards(s, [offering_id])).get(offering_id)


async def reindex_business(business_id: UUID) -> int:
    """Rebuild search documents (seeding/repair) and queue embeddings."""
    count = await indexer.reindex_business(business_id)
    enqueuer = _registry.optional(JobEnqueuer) if _registry else None  # type: ignore[type-abstract]
    await indexer.enqueue_embedding(enqueuer, business_id)
    return count


# ---------------------------------------------------------------- profile helpers (deterministic)


async def profile_block(business_id: UUID) -> RenderedProfile:
    return await profile.profile_block(business_id)


async def delivery_quote(business_id: UUID, area_or_pincode: str) -> DeliveryQuote:
    return profile.delivery_quote(await profile.load(business_id), area_or_pincode)


async def is_open(business_id: UUID, at: datetime | None = None) -> bool:
    return profile.is_open(await profile.load(business_id), at)


async def next_open(business_id: UUID, at: datetime | None = None) -> datetime | None:
    return profile.next_open(await profile.load(business_id), at)


async def earliest_delivery_date(
    business_id: UUID, at: datetime | None = None, notice_hours: int | None = None
) -> date:
    return profile.earliest_delivery_date(await profile.load(business_id), at, notice_hours)
