"""Seeds and `demo-reset` (POC_02 §7; POC_00 §7 criterion 7: reset < 1 minute)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reflexo.admin.seeding import reset
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id

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
    assert meera["connections"] == 7 and meera["bindings"] == 3 and meera["qa"] == 3  # 6 own + the bot mirror
    kurti = await _counts(db, "kurti-kraft")
    assert kurti["status"] == "active" and kurti["offerings"] == 10

    await reset(db, "full")
    meera = await _counts(db, "meeras-bakes")
    assert meera["status"] == "active" and meera["offerings"] == 6 and meera["variants"] == 6
    assert meera["onboarding"] == "live"

    await reset(db, "full")  # idempotent: same shape after a second reset
    assert (await _counts(db, "meeras-bakes"))["offerings"] == 6
    evalb = await _counts(db, "eval-bakes")
    assert evalb["offerings"] == 6 and evalb["connections"] == 3  # simulator, simulated WhatsApp (benchmark), mirror


async def test_seeded_businesses_are_searchable(db: Database) -> None:
    import reflexo.knowledge.api as knowledge

    report = await reset(db, "full")
    meera = report.businesses["meeras-bakes"]
    assert (await knowledge.search_catalog(meera, "choclate trufle"))[0].name == "Chocolate Truffle Cake"
    hits = await knowledge.search_knowledge(meera, "UPI chalega?")
    assert hits and "UPI" in hits[0].text
    kurti = report.businesses["kurti-kraft"]
    assert await knowledge.search_catalog(kurti, "kurti")


class FakeWorkflows:
    """Only `terminate` is used by the reset; `down` = Temporal unreachable."""

    def __init__(self, down: bool = False) -> None:
        self.down = down
        self.terminated: list[tuple[str, str]] = []

    async def terminate(self, workflow_id: str, reason: str) -> bool:
        if self.down:
            raise RuntimeError("Failed client connect")
        self.terminated.append((workflow_id, reason))
        return True

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"reset must not call {name}")


async def _waiting_approval(db: Database, slug: str, workflow_id: str, status: str = "pending") -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO approval_requests (id, business_id, kind, subject, preview, short_code, status,
                                                   expires_at, requested_by, workflow_id)
                    SELECT :id, id, 'automation_activation', '{}', '{}', :code, CAST(:st AS approval_status),
                           now() + interval '1 day', 'builder', :wf
                    FROM businesses WHERE slug = :slug"""),
            {"id": new_id(), "code": new_id().hex[:4], "st": status, "wf": workflow_id, "slug": slug},
        )


async def test_reset_stops_the_demo_businesses_live_workflows(db: Database) -> None:
    await reset(db, "full")
    await _waiting_approval(db, "meeras-bakes", "build-live-1")
    await _waiting_approval(db, "kurti-kraft", "build-live-2")
    await _waiting_approval(db, "meeras-bakes", "build-done", status="approved")  # finished: nothing to stop
    wf = FakeWorkflows()
    report = await reset(db, "full", workflows=wf)  # type: ignore[arg-type]
    assert sorted(wf.terminated) == [("build-live-1", "demo reset"), ("build-live-2", "demo reset")]
    assert (report.workflows_terminated, report.workflows_unreachable) == (2, False)
    async with db.service_session(ServiceReason.ADMIN) as s:
        left = await s.execute(
            text("""SELECT count(*) FROM approval_requests a JOIN businesses b ON b.id = a.business_id
                    WHERE b.slug IN ('meeras-bakes', 'kurti-kraft', 'eval-bakes')""")
        )
        assert left.scalar_one() == 0

    # Temporal down: reported, and the reset still completes
    await _waiting_approval(db, "meeras-bakes", "build-live-3")
    report = await reset(db, "onboarding", workflows=FakeWorkflows(down=True))  # type: ignore[arg-type]
    assert (report.workflows_terminated, report.workflows_unreachable) == (0, True)
    assert (await _counts(db, "meeras-bakes"))["status"] == "onboarding"
