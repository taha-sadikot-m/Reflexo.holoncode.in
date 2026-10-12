"""Public interface of the `knowledge` module. Other modules import only from here (POC_01 §4).

Business profile, order detail schemas, offerings, Q&A, change sets, search, imports. Spec: POC_05 §1-3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.contracts import BlockHydrator, EmbedderPort, JobEnqueuer, JobHandlerRegistry, JobRow
from reflexo.common.db import get_db
from reflexo.common.ports import Registry
from reflexo.knowledge import changesets, imports, indexer, playbook, profile, search
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
from reflexo.knowledge.resolve import OfferingRef, VariantRef, patch_settings, resolve_offerings

__all__ = [
    "BusinessProfile",
    "CatalogCard",
    "ChangeConflict",
    "ChangeSet",
    "ChangeSetPreview",
    "DeliveryQuote",
    "KnowledgeHit",
    "OfferingAttributes",
    "OfferingRef",
    "ParsedQuery",
    "RenderedProfile",
    "SpeculativeResult",
    "VariantInfo",
    "VariantRef",
    "add",
    "apply",
    "apply_now",
    "begin",
    "business_settings",
    "canonical_size",
    "cards_for",
    "delivery_quote",
    "detail_schemas",
    "earliest_delivery_date",
    "get_card",
    "get_profile",
    "get_qa",
    "is_open",
    "match_image_description",
    "next_open",
    "parse_query",
    "patch_settings",
    "preview",
    "profile_block",
    "qa_block",
    "register",
    "reindex_business",
    "resolve_offerings",
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


# ---------------------------------------------------------------- read models for desks and the output gate


async def cards_for(
    business_id: UUID, offering_ids: Sequence[UUID] = (), variant_ids: Sequence[UUID] = ()
) -> list[CatalogCard]:
    """Active cards for the given offerings plus the offerings owning the given variants (RLS-scoped)."""
    async with get_db().tenant_session(business_id) as s:
        ids = set(offering_ids)
        if variant_ids:
            rows = await s.execute(
                text("SELECT offering_id FROM offering_variants WHERE id = ANY(:ids) AND status = 'active'"),
                {"ids": list(variant_ids)},
            )
            ids |= set(rows.scalars())
        return list((await search.get_cards(s, list(ids))).values()) if ids else []


async def get_qa(business_id: UUID, qa_ids: Sequence[UUID], customer_id: UUID | None = None) -> list[KnowledgeHit]:
    """Q&A entries visible to this customer (same scope rules as search)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, question, answer, scope FROM qa_entries
                        WHERE id = ANY(:ids) AND status = 'active'
                          AND (scope = 'global' OR (scope = 'customer' AND customer_id = :cust)
                               OR (scope = 'temporary' AND valid_until > now()))"""),
                {"ids": list(qa_ids), "cust": customer_id},
            )
        ).all()
    return [
        KnowledgeHit(entity_type="qa", entity_id=r.id, title=r.question, text=r.answer, scope=str(r.scope))
        for r in rows
    ]


async def get_profile(business_id: UUID) -> BusinessProfile:
    return (await profile.load(business_id)).profile


async def qa_block(business_id: UUID, max_chars: int = 12000) -> list[KnowledgeHit]:
    """All global Q&A when small enough to put in the prompt (≈ 3k tokens); otherwise [] (use search)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, question, answer FROM qa_entries WHERE status = 'active' AND scope = 'global'
                        ORDER BY created_at LIMIT 200"""),
            )
        ).all()
    if sum(len(r.question) + len(r.answer) for r in rows) > max_chars:
        return []
    return [KnowledgeHit(entity_type="qa", entity_id=r.id, title=r.question, text=r.answer) for r in rows]


async def business_settings(business_id: UUID) -> dict[str, Any]:
    """Name, default language and `businesses.settings` (holding messages, COD rule, SLA...)."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("SELECT name, default_language, timezone, settings FROM businesses WHERE id = :b"),
                {"b": business_id},
            )
        ).one()
    return {
        "name": row.name,
        "default_language": row.default_language,
        "timezone": row.timezone,
        **(row.settings or {}),
    }


async def detail_schemas(business_id: UUID) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, name, applies_to, fields FROM order_detail_schemas WHERE status = 'active' ORDER BY name"
                )
            )
        ).all()
    return [{"id": str(r.id), "name": r.name, "applies_to": r.applies_to, "fields": r.fields} for r in rows]


__all__ += ["imports"]
__all__ += ["playbook"]  # playbook rules (ADR-015): match, rules, add_rule, FIELDS
