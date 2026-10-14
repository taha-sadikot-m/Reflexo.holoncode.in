"""Knowledge on a real database: hybrid search, live-price hydration, tenant isolation, change sets with
conflicts and undo, profile versioning, embeddings, hydrator (POC_05 §1; CHK-F02-K1..K3)."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from sqlalchemy import text

import reflexo.knowledge.api as knowledge
from reflexo.common.contracts import BlockHydrator, EmbedderPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.knowledge import indexer
from reflexo.pipeline import api as pipeline_api

pytestmark = pytest.mark.integration

SEED = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "config" / "seeds" / "meeras_bakes.yaml").read_text("utf-8")
)


class HashEmbedder:
    """Deterministic bag-of-words embedder (768 dims): shared words → similar vectors."""

    model = "test-hash-768"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str], *, task: str) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            vec = [0.0] * 768
            for word in t.lower().split():
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 768] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    knowledge.register(reg)
    return reg


async def make_shop(db: Database, *, catalog: bool = True) -> UUID:
    """Meera's Bakes profile + Q&A; catalogue built through a change set (exercises the real write path)."""
    bid = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO businesses (id, name, slug, status) VALUES (:id, 'Meera', :slug, 'active')"),
            {"id": bid, "slug": f"k-{bid}"},
        )
        await s.execute(
            text("INSERT INTO business_profiles (business_id, profile) VALUES (:b, CAST(:p AS jsonb))"),
            {"b": bid, "p": __import__("json").dumps(SEED["profile"])},
        )
    changes: list[tuple[Any, Any, UUID | None, dict[str, Any] | None]] = []
    for qa in SEED["qa"]:
        changes.append(
            (
                "qa",
                "create",
                None,
                {
                    "question": qa["question"],
                    "question_variants": qa["variants"],
                    "answer": qa["answer"],
                    "topic": qa["topic"],
                },
            )
        )
    if catalog:
        for item in SEED["catalog"]:
            oid = new_id()
            changes.append(
                (
                    "offering",
                    "create",
                    oid,
                    {
                        "name": item["name"],
                        "category": item["category"],
                        "description": item["description"],
                        "attributes": item.get("attributes", {}),
                        "aliases": item.get("aliases", []),
                        "base_price_paise": item.get("base_price_paise"),
                    },
                )
            )
            for v in item.get("variants", []):
                changes.append(
                    (
                        "variant",
                        "create",
                        None,
                        {"offering_id": str(oid), "name": v["name"], "price_paise": v["price_paise"]},
                    )
                )
    await knowledge.apply_now(bid, "seed", "Seed catalogue", changes)
    await knowledge.reindex_business(bid)  # profile sections
    return bid


async def offering_id(db: Database, bid: UUID, name: str) -> UUID:
    async with db.tenant_session(bid) as s:
        return (await s.execute(text("SELECT id FROM offerings WHERE name = :n"), {"n": name})).scalar_one()  # type: ignore[no-any-return]


async def variant_id(db: Database, bid: UUID, offering: str, name: str) -> UUID:
    async with db.tenant_session(bid) as s:
        return (  # type: ignore[no-any-return]
            await s.execute(
                text("""SELECT v.id FROM offering_variants v JOIN offerings o ON o.id = v.offering_id
                        WHERE o.name = :o AND v.name = :n"""),
                {"o": offering, "n": name},
            )
        ).scalar_one()


# ---------------------------------------------------------------- search (CHK-F02-K1)


@pytest.mark.parametrize(
    "query",
    ["choclate trufle", "Chocolate Truffle", "chocolate truffle 1kg price", "choco truffle", "truffle cake chahiye"],
)
async def test_truffle_found_for_spellings(registry: Registry, db: Database, query: str) -> None:
    bid = await make_shop(db)
    cards = await knowledge.search_catalog(bid, query)
    assert cards and cards[0].name == "Chocolate Truffle Cake", [c.name for c in cards]


async def test_size_hint_selects_variant_with_live_price(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    [top, *_] = await knowledge.search_catalog(bid, "chocolate truffle 1kg price")
    one_kg = next(v for v in top.variants if v.variant_id == top.matched_variant_id)
    assert (one_kg.name, one_kg.price_paise, one_kg.price_display) == ("1 kg", 95000, "₹950")


async def test_devanagari_query(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    names = [c.name for c in await knowledge.search_catalog(bid, "चॉकलेट केक")]
    assert "Chocolate Truffle Cake" in names[:2], names


async def test_budget_query_filters_by_variant_prices(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    cards = await knowledge.search_catalog(bid, "1000 ke andar cake", k=10)
    names = {c.name for c in cards}
    assert {"Chocolate Truffle Cake", "Pineapple Cake"} <= names
    assert "Red Velvet Cake" not in names  # ₹1,100
    assert "Custom Photo Cake" not in names  # price on request cannot meet a budget
    truffle = next(c for c in cards if c.name == "Chocolate Truffle Cake")
    best = next(v for v in truffle.variants if v.variant_id == truffle.matched_variant_id)
    assert best.name == "1 kg"  # the best option within budget


async def test_price_only_query_browses_catalogue(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    cards = await knowledge.search_catalog(bid, "₹500 tak", k=10)
    assert {c.name for c in cards} == {"Fudge Brownies", "Choco-chip Cookies"}


async def test_category_and_stock_filters(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    assert {c.name for c in await knowledge.search_catalog(bid, "chocolate", category="cookies")} == {
        "Choco-chip Cookies"
    }
    oid = await offering_id(db, bid, "Fudge Brownies")
    async with db.tenant_session(bid) as s:
        await s.execute(text("UPDATE offerings SET stock_mode = 'tracked', stock_qty = 0 WHERE id = :id"), {"id": oid})
    cards = await knowledge.search_catalog(bid, "brownies")
    assert cards[0].name == "Fudge Brownies" and not cards[0].in_stock
    assert await knowledge.search_catalog(bid, "brownies", in_stock_only=True) == []


async def test_price_on_request_card(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    [card, *_] = await knowledge.search_catalog(bid, "photo cake")
    assert card.name == "Custom Photo Cake" and card.price_on_request and card.variants == []


# ---------------------------------------------------------------- live prices (CHK-F02-K2) and isolation (K3)


async def test_price_change_visible_without_reindex(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    vid = await variant_id(db, bid, "Pineapple Cake", "1 kg")
    async with db.tenant_session(bid) as s:  # direct DB edit: the search document is not touched
        await s.execute(text("UPDATE offering_variants SET price_paise = 79900 WHERE id = :id"), {"id": vid})
    [card, *_] = await knowledge.search_catalog(bid, "pineapple cake")
    assert card.variants[0].price_paise == 79900


async def test_tenant_isolation(registry: Registry, db: Database) -> None:
    a = await make_shop(db)
    b = await make_shop(db, catalog=False)
    await knowledge.apply_now(b, "admin", "B catalogue", [("offering", "create", None, {"name": "Truffle Kurti"})])
    a_ids = {c.offering_id for c in await knowledge.search_catalog(a, "truffle", k=20)}
    b_cards = await knowledge.search_catalog(b, "truffle", k=20)
    assert [c.name for c in b_cards] == ["Truffle Kurti"]
    assert not a_ids & {c.offering_id for c in b_cards}
    async with db.tenant_session(b) as s:  # RLS: B's session cannot even see A's documents
        visible = await s.execute(text("SELECT count(*) FROM search_documents WHERE business_id = :a"), {"a": a})
        assert visible.scalar_one() == 0


# ---------------------------------------------------------------- knowledge search


async def test_knowledge_search_qa_and_profile(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    [hit, *_] = await knowledge.search_knowledge(bid, "delivery kahan tak hai")
    assert "10 km" in hit.text
    hours = await knowledge.search_knowledge(bid, "kab khulte ho sunday")
    assert any(h.entity_type == "profile" and h.title == "hours" and "Sun: 11:00–18:00" in h.text for h in hours)
    pay = await knowledge.search_knowledge(bid, "UPI chalega?")
    assert pay and "UPI" in pay[0].text


async def test_scoped_qa_visibility(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    cust, other = new_id(), new_id()
    async with db.tenant_session(bid) as s:
        for c in (cust, other):
            await s.execute(text("INSERT INTO customers (id, business_id) VALUES (:id, :b)"), {"id": c, "b": bid})
    await knowledge.apply_now(
        bid,
        "learning",
        "scoped answers",
        [
            (
                "qa",
                "create",
                None,
                {
                    "question": "Rahul ka special discount?",
                    "answer": "Rahul gets 10% off.",
                    "scope": "customer",
                    "customer_id": str(cust),
                    "topic": "discount_negotiation",
                },
            ),
            (
                "qa",
                "create",
                None,
                {
                    "question": "Diwali special hamper available?",
                    "answer": "Yes, till Sunday.",
                    "scope": "temporary",
                    "valid_until": "2020-01-01T00:00:00+00:00",
                    "topic": "catalog_info",
                },
            ),
        ],
    )
    assert any("10% off" in h.text for h in await knowledge.search_knowledge(bid, "special discount", customer_id=cust))
    assert not any(
        "10% off" in h.text for h in await knowledge.search_knowledge(bid, "special discount", customer_id=other)
    )
    assert not any("till Sunday" in h.text for h in await knowledge.search_knowledge(bid, "diwali hamper"))


async def test_speculative_never_raises(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    result = await knowledge.speculative(bid, "chocolate truffle price", budget_ms=5000)
    assert not result.timed_out and result.catalog[0].name == "Chocolate Truffle Cake"
    timed_out = await knowledge.speculative(bid, "chocolate truffle price", budget_ms=0)
    assert timed_out.timed_out and timed_out.catalog == []
    broken = await knowledge.speculative(new_id(), "anything", budget_ms=5000)  # unknown business: empty, no raise
    assert broken.catalog == [] and not broken.timed_out


# ---------------------------------------------------------------- change sets


async def test_change_set_preview_apply_undo(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    vid = await variant_id(db, bid, "Chocolate Truffle Cake", "1 kg")
    cs = await knowledge.begin(bid, "owner_command", "Truffle 1 kg price ₹1,000; add Mango Cake")
    await knowledge.add(cs, "variant", "update", vid, {"price_paise": 100000})
    mango = new_id()
    await knowledge.add(cs, "offering", "create", mango, {"name": "Mango Cake", "category": "cakes"})
    await knowledge.add(
        cs, "variant", "create", None, {"offering_id": str(mango), "name": "1 kg", "price_paise": 90000}
    )
    assert await knowledge.add(cs, "variant", "update", vid, {"price_paise": 95000}) is None  # no-op update skipped

    pv = await knowledge.preview(bid, cs.id)
    assert pv.lines == [
        "✏️ Chocolate Truffle Cake 1 kg price: ₹950 → ₹1,000",
        "➕ Mango Cake",
        "➕ Mango Cake 1 kg: ₹900",
    ]
    assert "Mango Cake" not in {c.name for c in await knowledge.search_catalog(bid, "mango cake")}  # drafts: no-op

    applied = await knowledge.apply(bid, cs.id)
    again = await knowledge.apply(bid, cs.id)  # duplicate YES: idempotent
    assert applied.status == again.status == "applied"
    [mango_card, *_] = await knowledge.search_catalog(bid, "mango")  # searchable at once (same transaction)
    assert mango_card.name == "Mango Cake" and mango_card.variants[0].price_paise == 90000
    async with db.tenant_session(bid) as s:
        events = (
            await s.execute(
                text("SELECT payload FROM domain_events WHERE type = 'knowledge.changed' AND correlation_id = :c"),
                {"c": cs.id},
            )
        ).all()
        jobs = (
            await s.execute(
                text("SELECT count(*) FROM jobs WHERE queue = 'knowledge.reindex' AND business_id = :b"), {"b": bid}
            )
        ).scalar_one()
    assert len(events) == 1 and events[0].payload["origin"] == "owner_command"
    assert jobs == 1  # deduped across change sets

    undo = await knowledge.revert(bid)
    assert undo.revert_of == cs.id
    truffle = (await knowledge.search_catalog(bid, "chocolate truffle 1kg"))[0]
    assert next(v.price_paise for v in truffle.variants if v.variant_id == vid) == 95000
    assert "Mango Cake" not in {c.name for c in await knowledge.search_catalog(bid, "mango cake")}  # undo archives
    async with db.tenant_session(bid) as s:
        status = await s.execute(text("SELECT status FROM knowledge_change_sets WHERE id = :id"), {"id": cs.id})
        assert str(status.scalar_one()) == "reverted"


async def test_conflict_refuses_whole_set(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    vid = await variant_id(db, bid, "Red Velvet Cake", "1 kg")
    oid = await offering_id(db, bid, "Pineapple Cake")
    cs = await knowledge.begin(bid, "import", "Price list update")
    await knowledge.add(cs, "offering", "update", oid, {"description": "Now with real pineapple chunks"})
    await knowledge.add(cs, "variant", "update", vid, {"price_paise": 120000})
    async with db.tenant_session(bid) as s:  # someone changes the price meanwhile (admin panel)
        await s.execute(text("UPDATE offering_variants SET price_paise = 115000 WHERE id = :id"), {"id": vid})
    with pytest.raises(knowledge.ChangeConflict) as err:
        await knowledge.apply(bid, cs.id)
    assert err.value.fields == ["price_paise"]
    async with db.tenant_session(bid) as s:
        desc = (await s.execute(text("SELECT description FROM offerings WHERE id = :id"), {"id": oid})).scalar_one()
        status = await s.execute(text("SELECT status FROM knowledge_change_sets WHERE id = :id"), {"id": cs.id})
        assert str(status.scalar_one()) == "draft"
    assert desc != "Now with real pineapple chunks"  # first item rolled back too


async def test_soft_delete_and_restore(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    oid = await offering_id(db, bid, "Choco-chip Cookies")
    cs = await knowledge.begin(bid, "owner_command", "Remove cookies")
    await knowledge.add(cs, "offering", "delete", oid)
    assert (await knowledge.preview(bid, cs.id)).lines == ["🗑 Remove: Choco-chip Cookies"]
    await knowledge.apply(bid, cs.id)
    assert await knowledge.search_catalog(bid, "cookies") == []
    assert await knowledge.get_card(bid, oid) is None
    await knowledge.revert(bid)
    assert (await knowledge.search_catalog(bid, "cookies"))[0].offering_id == oid


async def test_undo_twice_steps_back_twice(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    vid = await variant_id(db, bid, "Fudge Brownies", "Box of 6")
    oid = await offering_id(db, bid, "Choco-chip Cookies")
    await knowledge.apply_now(
        bid, "owner_command", "Brownies ₹450", [("variant", "update", vid, {"price_paise": 45000})]
    )
    await knowledge.apply_now(bid, "owner_command", "Remove cookies", [("offering", "delete", oid, None)])
    assert (await knowledge.revert(bid)).summary == "Undo: Remove cookies"
    assert (await knowledge.revert(bid)).summary == "Undo: Brownies ₹450"  # not a redo of the first undo
    async with db.tenant_session(bid) as s:
        price = (await s.execute(text("SELECT price_paise FROM offering_variants WHERE id = :v"), {"v": vid})).scalar()
    assert price == 42000 and await knowledge.get_card(bid, oid) is not None


async def test_undo_refused_when_changed_afterwards(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    vid = await variant_id(db, bid, "Fudge Brownies", "Box of 6")
    await knowledge.apply_now(
        bid, "owner_command", "Brownies ₹450", [("variant", "update", vid, {"price_paise": 45000})]
    )
    async with db.tenant_session(bid) as s:
        await s.execute(text("UPDATE offering_variants SET price_paise = 47000 WHERE id = :id"), {"id": vid})
    with pytest.raises(knowledge.ChangeConflict):
        await knowledge.revert(bid)


async def test_unknown_fields_rejected(registry: Registry, db: Database) -> None:
    bid = await make_shop(db, catalog=False)
    cs = await knowledge.begin(bid, "admin", "bad")
    with pytest.raises(ValueError, match="unknown offering fields"):
        await knowledge.add(cs, "offering", "create", None, {"name": "X", "price": 100})


async def test_profile_change_bumps_version(registry: Registry, db: Database) -> None:
    bid = await make_shop(db, catalog=False)
    before = await knowledge.profile_block(bid)
    profile = {**SEED["profile"], "delivery": {**SEED["profile"]["delivery"], "notice_hours": 48}}
    cs = await knowledge.begin(bid, "owner_command", "Notice 48 h")
    await knowledge.add(cs, "profile", "update", None, {"profile": profile})
    assert (await knowledge.preview(bid, cs.id)).lines == ["✏️ delivery.notice hours: 24 → 48"]
    await knowledge.apply(bid, cs.id)
    after = await knowledge.profile_block(bid)
    assert after.version == before.version + 1
    assert "48 h notice" in after.text and "24 h notice" in before.text
    quote = await knowledge.delivery_quote(bid, "411038")
    assert quote.serviceable and quote.notice_hours == 48
    hits = await knowledge.search_knowledge(bid, "delivery notice")
    assert any("48 h notice" in h.text for h in hits)  # profile documents re-indexed in the same transaction


# ---------------------------------------------------------------- embeddings


async def test_embeddings_fill_and_invalidate(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    embedder = HashEmbedder()
    done = await indexer.embed_pending(bid, embedder)
    assert done > 10
    async with db.tenant_session(bid) as s:
        missing = (await s.execute(text("SELECT count(*) FROM search_documents WHERE embedding IS NULL"))).scalar_one()
    assert missing == 0
    assert await indexer.embed_pending(bid, embedder) == 0  # nothing left

    registry.provide(EmbedderPort, embedder)  # type: ignore[type-abstract]
    cards = await knowledge.search_catalog(bid, "gooey fudge")  # vector arm active
    assert cards[0].name == "Fudge Brownies"

    oid = await offering_id(db, bid, "Fudge Brownies")
    await knowledge.apply_now(bid, "admin", "rename", [("offering", "update", oid, {"description": "Walnut brownies"})])
    async with db.tenant_session(bid) as s:
        emb = (
            await s.execute(text("SELECT embedding IS NULL FROM search_documents WHERE entity_id = :id"), {"id": oid})
        ).scalar_one()
    assert emb  # changed text → stale embedding cleared, re-embedded by the next job


async def test_failing_embedder_degrades_to_lexical(registry: Registry, db: Database) -> None:
    class Broken:
        model = "broken"

        async def embed(self, texts: list[str], *, task: str) -> list[list[float]]:
            raise TimeoutError("model down")

    bid = await make_shop(db)
    registry.provide(EmbedderPort, Broken())  # type: ignore[type-abstract]
    assert (await knowledge.search_catalog(bid, "choclate trufle"))[0].name == "Chocolate Truffle Cake"


# ---------------------------------------------------------------- hydrator


async def test_block_hydrator(registry: Registry, db: Database) -> None:
    bid = await make_shop(db)
    hydrator = registry.require(BlockHydrator)  # type: ignore[type-abstract]
    oid = await offering_id(db, bid, "Chocolate Truffle Cake")
    vid = await variant_id(db, bid, "Chocolate Truffle Cake", "500 g")
    full = await hydrator.product(bid, oid, None)
    one = await hydrator.product(bid, oid, vid)
    assert [line.price_display for line in full.lines] == ["₹550", "₹950"]
    assert [line.name for line in one.lines] == ["500 g"]
    with pytest.raises(LookupError):
        await hydrator.product(bid, oid, new_id())
    with pytest.raises(LookupError, match="commerce"):
        await hydrator.payment_link(bid, new_id())
