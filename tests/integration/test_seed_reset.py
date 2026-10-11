"""Seeds and `demo-reset` (POC_02 §7; POC_00 §7 criterion 7: reset < 1 minute)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from reflexo.admin.seeding import reset
from reflexo.common.db import Database, ServiceReason

pytestmark = pytest.mark.integration


async def _counts(db: Database, slug: str) -> dict[str, object]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        row = (
            await s.execute(
                text("""
            SELECT b.status,
                   (SELECT count(*) FROM offerings o WHERE o.business_id = b.id) AS offerings,
                   (SELECT count(*) FROM offering_variants v WHERE v.business_id = b.id) AS variants,
                   (SELECT count(*) FROM channel_connections c WHERE c.business_id = b.id) AS connections,
                   (SELECT count(*) FROM owner_channel_bindings ob WHERE ob.business_id = b.id) AS bindings,
                   (SELECT count(*) FROM qa_entries q WHERE q.business_id = b.id) AS qa,
                   (SELECT state FROM onboarding_sessions os WHERE os.business_id = b.id) AS onboarding
            FROM businesses b WHERE b.slug = :slug"""),
                {"slug": slug},
            )
        ).one()
    return dict(row._mapping)


async def test_reset_onboarding_then_full_is_repeatable(db: Database) -> None:
    report = await reset(db, "onboarding")
    assert report.seconds < 60
    meera = await _counts(db, "meeras-bakes")
    assert meera["status"] == "onboarding" and meera["offerings"] == 0 and meera["onboarding"] == "sources_requested"
    assert meera["connections"] == 6 and meera["bindings"] == 3 and meera["qa"] == 3
    kurti = await _counts(db, "kurti-kraft")
    assert kurti["status"] == "active" and kurti["offerings"] == 10

    await reset(db, "full")
    meera = await _counts(db, "meeras-bakes")
    assert meera["status"] == "active" and meera["offerings"] == 6 and meera["variants"] == 6
    assert meera["onboarding"] == "live"

    await reset(db, "full")  # idempotent: same shape after a second reset
    assert (await _counts(db, "meeras-bakes"))["offerings"] == 6
    evalb = await _counts(db, "eval-bakes")
    assert evalb["offerings"] == 6 and evalb["connections"] == 1


async def test_seeded_businesses_are_searchable(db: Database) -> None:
    import reflexo.knowledge.api as knowledge

    report = await reset(db, "full")
    meera = report.businesses["meeras-bakes"]
    assert (await knowledge.search_catalog(meera, "choclate trufle"))[0].name == "Chocolate Truffle Cake"
    hits = await knowledge.search_knowledge(meera, "UPI chalega?")
    assert hits and "UPI" in hits[0].text
    kurti = report.businesses["kurti-kraft"]
    assert await knowledge.search_catalog(kurti, "kurti")
