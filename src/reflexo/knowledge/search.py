"""Hybrid search over `search_documents` with reciprocal-rank fusion (harvested H13, rewritten; POC_05 §1.5).

Three arms per business, exact (no ANN index; catalogues are small): full text (`simple` config, OR of terms),
word-level trigram on titles (misspellings, Hinglish spellings), and pgvector cosine when an embedder exists.
Hits are hydrated from live rows, so a price edited a second ago is what the customer sees (CHK-F02-K2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import get_settings
from reflexo.common.contracts import EmbedderPort
from reflexo.common.db import get_db
from reflexo.common.logging import get_logger
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.knowledge import profile as profile_mod
from reflexo.knowledge.models import CatalogCard, KnowledgeHit, SpeculativeResult, VariantInfo
from reflexo.knowledge.query import ParsedQuery, canonical_size, parse_query

log = get_logger(__name__)

RRF_K = 60
ARM_LIMIT = 20
EMBED_CACHE_TTL_S = 3600
EMBED_TIMEOUT_S = 2.0

_ARMS_SQL = """
WITH fts AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank(tsv, to_tsquery('simple', :tsq)) DESC) AS r
    FROM search_documents
    WHERE business_id = :b AND entity_type = ANY(:types) AND tsv @@ to_tsquery('simple', :tsq)
    ORDER BY ts_rank(tsv, to_tsquery('simple', :tsq)) DESC LIMIT :lim
),
trg AS (
    SELECT id, row_number() OVER (ORDER BY word_similarity(:t, title) DESC) AS r
    FROM search_documents
    WHERE business_id = :b AND entity_type = ANY(:types) AND :t <% title
    ORDER BY word_similarity(:t, title) DESC LIMIT :lim
)
{vec_cte}
SELECT d.entity_type, d.entity_id, SUM(1.0 / ({rrf_k} + u.r)) AS score
FROM (SELECT * FROM fts UNION ALL SELECT * FROM trg {vec_union}) u
JOIN search_documents d ON d.id = u.id
GROUP BY d.entity_type, d.entity_id
ORDER BY score DESC
"""

_VEC_CTE = """,
vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:v AS vector)) AS r
    FROM search_documents
    WHERE business_id = :b AND entity_type = ANY(:types) AND embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:v AS vector) LIMIT :lim
)"""


def _tsquery(terms: Sequence[str]) -> str:
    # terms come from common.text.normalize (no punctuation/symbols), so quoting is safe; OR keeps recall high
    # and ts_rank still prefers documents that match more terms.
    return " | ".join(f"'{t}'" for t in terms if t) or "''"


# ---------------------------------------------------------------- query embeddings


async def _query_vector(query: str, embedder: EmbedderPort | None, *, allow_remote: bool) -> list[float] | None:
    if embedder is None or not query:
        return None
    key = "cache:emb:" + hashlib.sha1(f"{embedder.model}\n{query}".encode()).hexdigest()  # noqa: S324 - cache key
    try:
        cached = await get_redis().call("get", key)
        if cached:
            return list(json.loads(cached))
    except RedisUnavailable:
        pass
    if not allow_remote:
        return None
    try:
        [vec] = await asyncio.wait_for(embedder.embed([query], task="query"), EMBED_TIMEOUT_S)
    except Exception as exc:  # a slow/failed embedder degrades search to lexical, never fails it
        log.warning("query_embedding_failed", error=type(exc).__name__)
        return None
    try:
        await get_redis().call("set", key, json.dumps(vec), ex=EMBED_CACHE_TTL_S)
    except RedisUnavailable:
        pass
    return vec


async def _ranked(
    s: AsyncSession, business_id: UUID, pq: ParsedQuery, types: list[str], vector: list[float] | None, limit: int
) -> list[tuple[str, UUID, float]]:
    await s.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :v, true)"),
        {"v": str(get_settings().search_trgm_threshold)},
    )
    sql = _ARMS_SQL.format(
        vec_cte=_VEC_CTE if vector is not None else "",
        vec_union="UNION ALL SELECT * FROM vec" if vector is not None else "",
        rrf_k=RRF_K,
    )
    params: dict[str, Any] = {"b": business_id, "types": types, "t": pq.text, "tsq": _tsquery(pq.terms), "lim": limit}
    if vector is not None:
        params["v"] = "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
    rows = (await s.execute(text(sql), params)).all()
    return [(str(r.entity_type), r.entity_id, float(r.score)) for r in rows]


# ---------------------------------------------------------------- catalog


_CARDS_SQL = text("""
    SELECT o.id, o.name, o.description, o.category, o.attributes, o.base_price_paise, o.stock_mode, o.stock_qty,
           o.image_media_id,
           COALESCE(json_agg(json_build_object('id', v.id, 'name', v.name, 'price', v.price_paise,
                                               'stock', v.stock_qty) ORDER BY v.price_paise)
                    FILTER (WHERE v.id IS NOT NULL AND v.status = 'active'), '[]') AS variants
    FROM offerings o LEFT JOIN offering_variants v ON v.offering_id = o.id
    WHERE o.id = ANY(:ids) AND o.status = 'active'
    GROUP BY o.id
""")


def _card(row: Any, score: float = 0.0) -> CatalogCard:
    tracked = str(row.stock_mode) != "untracked"

    def in_stock(qty: int | None) -> bool:
        if not tracked:
            return True
        effective = qty if qty is not None else row.stock_qty
        return effective is None or effective > 0  # integration stock unknown → sellable

    variants = [
        VariantInfo(
            variant_id=UUID(v["id"]), name=v["name"], price_paise=int(v["price"]), in_stock=in_stock(v["stock"])
        )
        for v in row.variants
    ]
    if not variants and row.base_price_paise is not None:
        variants = [
            VariantInfo(variant_id=None, name="", price_paise=int(row.base_price_paise), in_stock=in_stock(None))
        ]
    return CatalogCard(
        offering_id=row.id,
        name=row.name,
        description=row.description,
        category=row.category,
        attributes=row.attributes or {},
        variants=variants,
        price_on_request=not variants,
        in_stock=any(v.in_stock for v in variants) if variants else in_stock(None),
        image_media_id=row.image_media_id,
        score=score,
    )


async def get_cards(s: AsyncSession, offering_ids: Sequence[UUID]) -> dict[UUID, CatalogCard]:
    rows = (await s.execute(_CARDS_SQL, {"ids": list(offering_ids)})).all()
    return {r.id: _card(r) for r in rows}


def _apply_filters(
    card: CatalogCard, pq: ParsedQuery, max_price: int | None, category: str | None, in_stock_only: bool
) -> CatalogCard | None:
    if category and (card.category or "").casefold() != category.casefold():
        return None
    if in_stock_only and not card.in_stock:
        return None
    candidates = [v for v in card.variants if not in_stock_only or v.in_stock]
    if max_price is not None:
        candidates = [v for v in candidates if v.price_paise is not None and v.price_paise <= max_price]
        if not candidates:
            return None  # includes price-on-request items: the budget cannot be checked
    matched = None
    if pq.size_hint:
        matched = next((v for v in candidates if canonical_size(v.name) == pq.size_hint), None)
    if matched is None and max_price is not None:
        matched = max(candidates, key=lambda v: v.price_paise or 0)  # best option within the budget
    if matched is not None and matched.variant_id is not None:
        bonus = 0.005 if pq.size_hint and canonical_size(matched.name) == pq.size_hint else 0.0
        return card.model_copy(update={"matched_variant_id": matched.variant_id, "score": card.score + bonus})
    return card


async def search_catalog(
    business_id: UUID,
    query: str,
    *,
    max_price_paise: int | None = None,
    category: str | None = None,
    in_stock_only: bool = False,
    k: int | None = None,
    embedder: EmbedderPort | None = None,
    allow_remote_embedding: bool = True,
) -> list[CatalogCard]:
    pq = parse_query(query)
    max_price = max_price_paise if max_price_paise is not None else pq.max_price_paise
    limit = k or get_settings().search_k
    vector = await _query_vector(pq.normalized, embedder, allow_remote=allow_remote_embedding)
    async with get_db().tenant_session(business_id) as s:
        if pq.text:
            ranked = await _ranked(s, business_id, pq, ["offering"], vector, ARM_LIMIT * (2 if max_price else 1))
            scores = {eid: score for _, eid, score in ranked}
        else:  # "₹1000 tak", "2 kg wala": browse the catalogue with the filters only
            ids = (
                await s.execute(
                    text(
                        "SELECT id FROM offerings WHERE business_id = :b AND status = 'active' ORDER BY name LIMIT 200"
                    ),
                    {"b": business_id},
                )
            ).scalars()
            scores = dict.fromkeys(ids, 0.0)
        if not scores:
            return []
        cards = await get_cards(s, list(scores))
    out: list[CatalogCard] = []
    for eid, score in scores.items():
        card = cards.get(eid)
        if card is None:
            continue  # archived/hidden since indexing
        filtered = _apply_filters(card.model_copy(update={"score": score}), pq, max_price, category, in_stock_only)
        if filtered is not None:
            out.append(filtered)
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]


async def match_image_description(
    business_id: UUID, description: str, visible_text: str | None = None, *, embedder: EmbedderPort | None = None
) -> list[CatalogCard]:
    """Customer/in-call photos are matched by their vision description (POC_05 §1.5, image matching)."""
    return await search_catalog(business_id, f"{description} {visible_text or ''}".strip(), k=3, embedder=embedder)


# ---------------------------------------------------------------- knowledge (Q&A + profile)


async def search_knowledge(
    business_id: UUID,
    query: str,
    *,
    customer_id: UUID | None = None,
    k: int = 3,
    embedder: EmbedderPort | None = None,
    allow_remote_embedding: bool = True,
) -> list[KnowledgeHit]:
    pq = parse_query(query)
    vector = await _query_vector(pq.normalized, embedder, allow_remote=allow_remote_embedding)
    async with get_db().tenant_session(business_id) as s:
        ranked = await _ranked(s, business_id, pq, ["qa", "profile"], vector, ARM_LIMIT)
        qa_ids = [eid for et, eid, _ in ranked if et == "qa"]
        qa_rows = {
            r.id: r
            for r in (
                await s.execute(
                    text("""SELECT id, question, answer, scope FROM qa_entries
                            WHERE id = ANY(:ids) AND status = 'active'
                              AND (scope = 'global'
                                   OR (scope = 'customer' AND customer_id = :cust)
                                   OR (scope = 'temporary' AND valid_until > now()))"""),
                    {"ids": qa_ids, "cust": customer_id},
                )
            ).all()
        }
        lp = await profile_mod.load(business_id, s) if any(et == "profile" for et, _, _ in ranked) else None
    sections = {profile_mod.section_entity_id(business_id, name): name for name in profile_mod.SECTIONS}
    hits: list[KnowledgeHit] = []
    for et, eid, score in ranked:
        if et == "qa" and eid in qa_rows:
            r = qa_rows[eid]
            hits.append(KnowledgeHit(entity_type="qa", entity_id=eid, title=r.question, text=r.answer,
                                     scope=str(r.scope), score=score))  # fmt: skip
        elif et == "profile" and lp is not None and eid in sections:
            name = sections[eid]
            hits.append(KnowledgeHit(entity_type="profile", entity_id=eid, title=name,
                                     text=profile_mod.section_text(lp, name), score=score))  # fmt: skip
        if len(hits) >= k:
            break
    return hits


# ---------------------------------------------------------------- speculative retrieval


async def speculative(
    business_id: UUID,
    text_in: str,
    *,
    customer_id: UUID | None = None,
    budget_ms: int | None = None,
    embedder: EmbedderPort | None = None,
) -> SpeculativeResult:
    """Retrieval started while the turn is still coalescing (POC_04). Never raises; empty on timeout or error.

    Uses a query embedding only if it is already cached: a remote embedding call does not fit the budget."""
    budget = (budget_ms if budget_ms is not None else get_settings().speculative_budget_ms) / 1000
    started = time.perf_counter()
    try:
        catalog, knowledge = await asyncio.wait_for(
            asyncio.gather(
                search_catalog(business_id, text_in, k=3, embedder=embedder, allow_remote_embedding=False),
                search_knowledge(
                    business_id, text_in, customer_id=customer_id, k=3, embedder=embedder, allow_remote_embedding=False
                ),
            ),
            budget,
        )
        return SpeculativeResult(
            catalog=catalog, knowledge=knowledge, elapsed_ms=int((time.perf_counter() - started) * 1000)
        )
    except TimeoutError:
        return SpeculativeResult(timed_out=True, elapsed_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:
        log.warning("speculative_retrieval_failed", error=type(exc).__name__)
        return SpeculativeResult(elapsed_ms=int((time.perf_counter() - started) * 1000))
