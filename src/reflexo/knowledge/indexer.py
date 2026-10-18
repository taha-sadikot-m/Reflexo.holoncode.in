"""Search documents (POC_05 §1.5): one row per active offering, Q&A entry and profile section.

Text columns are written in the caller's transaction, so a new or renamed product is searchable the moment the
change commits. Embeddings are filled later by job `knowledge.reindex` (batches of 50) when an embedder exists.
Prices and stock are never written into documents; search hydrates them from the rows (D-041).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.contracts import EmbedderPort, JobEnqueuer
from reflexo.common.db import get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx
from reflexo.common.logging import get_logger
from reflexo.knowledge import profile as profile_mod

log = get_logger(__name__)
EMBED_BATCH = 50

_UPSERT = text("""
    INSERT INTO search_documents (id, business_id, entity_type, entity_id, title, content)
    VALUES (:id, :b, :et, :eid, :title, :content)
    ON CONFLICT (entity_type, entity_id) DO UPDATE SET
      title = EXCLUDED.title, content = EXCLUDED.content, updated_at = now(),
      -- a changed text invalidates its embedding; unchanged text keeps it (no needless re-embedding)
      embedding = CASE WHEN search_documents.title = EXCLUDED.title AND search_documents.content = EXCLUDED.content
                       THEN search_documents.embedding END,
      embedding_model = CASE WHEN search_documents.title = EXCLUDED.title
                              AND search_documents.content = EXCLUDED.content
                             THEN search_documents.embedding_model END
""")


def _attributes_text(attrs: dict[str, Any]) -> str:
    words: list[str] = []
    if attrs.get("eggless_available"):
        words.append("eggless available eggless option")
    for key in ("flavours", "weights"):
        if attrs.get(key):
            words.append(f"{key}: " + ", ".join(str(x) for x in attrs[key]))
    if attrs.get("serves"):
        words.append(f"serves {attrs['serves']}")
    return ". ".join(words)


async def _upsert(
    s: AsyncSession, business_id: UUID, entity_type: str, entity_id: UUID, title: str, content: str
) -> None:
    await s.execute(
        _UPSERT,
        {"id": new_id(), "b": business_id, "et": entity_type, "eid": entity_id, "title": title, "content": content},
    )


async def _remove(s: AsyncSession, entity_type: str, entity_id: UUID) -> None:
    await s.execute(
        text("DELETE FROM search_documents WHERE entity_type = :et AND entity_id = :eid"),
        {"et": entity_type, "eid": entity_id},
    )


async def index_offering(s: AsyncSession, business_id: UUID, offering_id: UUID) -> None:
    row = (
        await s.execute(
            text("""SELECT o.name, o.description, o.category, o.attributes, o.aliases, o.status, o.kind,
                           COALESCE(array_agg(v.name ORDER BY v.price_paise) FILTER (WHERE v.status = 'active'),
                                    '{}') AS variant_names
                    FROM offerings o LEFT JOIN offering_variants v ON v.offering_id = o.id
                    WHERE o.id = :id GROUP BY o.id"""),
            {"id": offering_id},
        )
    ).first()
    if row is None or str(row.status) != "active":
        await _remove(s, "offering", offering_id)
        return
    title = " · ".join([row.name, *row.aliases])
    content = ". ".join(
        x
        for x in (
            row.description or "",
            f"category {row.category}" if row.category else "",
            _attributes_text(row.attributes or {}),
            ("sizes " + ", ".join(row.variant_names)) if row.variant_names else "",
        )
        if x
    )
    await _upsert(s, business_id, "offering", offering_id, title, content or row.name)


async def index_qa(s: AsyncSession, business_id: UUID, qa_id: UUID) -> None:
    row = (
        await s.execute(
            text("SELECT question, question_variants, answer, status, scope FROM qa_entries WHERE id = :id"),
            {"id": qa_id},
        )
    ).first()
    if row is None or str(row.status) != "active" or str(row.scope) == "none":
        await _remove(s, "qa", qa_id)
        return
    await _upsert(s, business_id, "qa", qa_id, " · ".join([row.question, *row.question_variants]), row.answer)


async def index_profile(s: AsyncSession, business_id: UUID) -> None:
    lp = await profile_mod.load(business_id, s)
    for section, keywords in profile_mod.SECTIONS.items():
        body = profile_mod.section_text(lp, section)
        eid = profile_mod.section_entity_id(business_id, section)
        if body.strip():
            await _upsert(s, business_id, "profile", eid, f"{section} {keywords}", body)
        else:
            await _remove(s, "profile", eid)


async def index_entities(s: AsyncSession, business_id: UUID, refs: Iterable[tuple[str, UUID]]) -> None:
    """Re-index the documents affected by a change (variants re-index their offering)."""
    seen: set[tuple[str, UUID]] = set()
    for entity_type, entity_id in refs:
        if (entity_type, entity_id) in seen:
            continue
        seen.add((entity_type, entity_id))
        if entity_type == "offering":
            await index_offering(s, business_id, entity_id)
        elif entity_type == "qa":
            await index_qa(s, business_id, entity_id)
        elif entity_type == "profile":
            await index_profile(s, business_id)


async def reindex_business(business_id: UUID) -> int:
    """Rebuild every document of a business (seeding, admin repair). Returns the number of documents."""
    async with get_db().tenant_session(business_id) as s:
        offerings = (
            await s.execute(text("SELECT id FROM offerings WHERE business_id = :b"), {"b": business_id})
        ).scalars()
        for oid in list(offerings):
            await index_offering(s, business_id, oid)
        qas = (await s.execute(text("SELECT id FROM qa_entries WHERE business_id = :b"), {"b": business_id})).scalars()
        for qid in list(qas):
            await index_qa(s, business_id, qid)
        has_profile = (
            await s.execute(text("SELECT 1 FROM business_profiles WHERE business_id = :b"), {"b": business_id})
        ).scalar_one_or_none()
        if has_profile:
            await index_profile(s, business_id)
        count = (
            await s.execute(text("SELECT count(*) FROM search_documents WHERE business_id = :b"), {"b": business_id})
        ).scalar_one()
    return int(count)


async def enqueue_embedding(enqueuer: JobEnqueuer | None, business_id: UUID, s: AsyncSession | None = None) -> None:
    if enqueuer is None:
        return
    await enqueuer.enqueue(
        "knowledge.reindex",
        {"business_id": str(business_id)},
        business_id=business_id,
        priority=PriorityClass.P2,
        dedupe_key=f"knowledge.reindex:{business_id}",
        delay_s=1.0,  # let a burst of changes land first
        session=s,
    )


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


async def embed_pending(business_id: UUID, embedder: EmbedderPort | None) -> int:
    """Fill missing embeddings for a business in batches. Returns how many documents were embedded."""
    if embedder is None:
        return 0
    done = 0
    for _ in range(200):  # bounded: 10k documents per job run; the job re-runs if more remain
        async with get_db().tenant_session(business_id) as s:
            rows = (
                await s.execute(
                    text("""SELECT id, title, content FROM search_documents
                            WHERE business_id = :b AND embedding IS NULL ORDER BY updated_at LIMIT :n"""),
                    {"b": business_id, "n": EMBED_BATCH},
                )
            ).all()
        if not rows:
            return done
        ctx = CallCtx(business_id=business_id, purpose="embedding", run_ref=f"reindex:{business_id}")
        vectors = await embedder.embed([f"{r.title}\n{r.content}" for r in rows], task="document", ctx=ctx)
        if len(vectors) != len(rows):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(rows)} documents")
        async with get_db().tenant_session(business_id) as s:
            for r, vec in zip(rows, vectors, strict=True):
                # guarded by the text: a document edited while we embedded stays NULL for the next round
                await s.execute(
                    text("""UPDATE search_documents SET embedding = CAST(:v AS vector), embedding_model = :m
                            WHERE id = :id AND title = :t AND content = :c"""),
                    {"v": _vector_literal(vec), "m": embedder.model, "id": r.id, "t": r.title, "c": r.content},
                )
        done += len(rows)
        log.info("knowledge_embedded", business_id=str(business_id), batch=len(rows))
    return done
