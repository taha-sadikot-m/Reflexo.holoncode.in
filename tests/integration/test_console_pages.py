"""The operator console pages added by the stage 2 audit (BETA_13 §3, §4): Costs, Benchmarks, Access logs, and the
reviewer's view of them (only the showcase shops, never sign-ups or sandboxes)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.vault.api as vault
from reflexo.admin import access, benchmark
from reflexo.api.main import app
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry
from reflexo.common.timeutil import utcnow
from tests.integration import test_customer_desk as cd
from tests.integration.test_customer_desk import PRIMARY, desk, make_bakery, reply, turn_for
from tests.integration.test_gateway import make_shop

fake, notes, registry = cd.fake, cd.notes, cd.registry
_generous_speculative_budget = cd._generous_speculative_budget
pytestmark = pytest.mark.integration


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient, user: str = "admin", password: str = "test-admin-password") -> str:
    resp = await client.post("/admin/api/login", json={"username": user, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["csrf"])


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def call_row(db: Database, business_id: UUID, feature: str, cost: int) -> None:
    """A metered model call outside any turn (an embedding, a setup call)."""
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO model_calls (id, business_id, feature, kind, purpose, provider, model, input_tokens,
                                             cost_micro_inr, price_version, duration_ms, status)
                    VALUES (:id, :b, :f, 'embed', 'audit_test', 'fake', 'fake-embed', 100, :c, 'test', 5, 'ok')"""),
            {"id": new_id(), "b": business_id, "f": feature, "c": cost},
        )


async def test_the_costs_page_adds_up_the_metered_rows(
    registry: Registry, db: Database, fake: agent.FakeProvider, client: httpx.AsyncClient
) -> None:
    bid, pnid = await make_bakery(db)
    fake.queue(PRIMARY, agent.FakeReply(tool_calls=reply("send", "Hello!", topic="greeting").tool_calls,
                                        input_tokens=4000, cached_input_tokens=1000, output_tokens=40))  # fmt: skip
    handle = await turn_for(registry, db, pnid, "919811100091", "Hi")
    await desk(registry).run_turn(handle)
    await call_row(db, bid, "embedding", 2_000)
    other = await make_shop(db)
    await call_row(db, other.business_id, "setup", 7_000)
    chat = sum(r.cost_micro_inr for r in await q(db, "SELECT cost_micro_inr FROM model_calls WHERE turn_id = :t",
                                                  t=handle.ref.turn_id))  # fmt: skip
    assert chat > 0

    await login(client)
    page = (await client.get("/admin/api/costs", params={"business_id": str(bid), "group_by": "feature"})).json()
    recorded = await q(db, "SELECT feature, sum(cost_micro_inr) AS c FROM model_calls WHERE business_id = :b "
                           "GROUP BY feature", b=bid)  # fmt: skip
    totals = page["totals"]
    assert totals["cost_micro_inr"] == sum(int(r.c) for r in recorded)  # every metered call, in a turn or not
    assert totals["chat_turns"] == 1 and totals["conversations"] == 1 and totals["cost_per_turn_micro_inr"] == chat
    assert {r["key"]: r["cost_micro_inr"] for r in page["rows"]} == {r.feature: int(r.c) for r in recorded}
    assert {r["key"] for r in page["rows"]} >= {"chat", "embedding"}
    assert page["fx"]["usd_inr"] > 0 and page["price_version"]
    [cap] = page["caps"]
    assert cap["business_id"] == str(bid) and cap["today_micro_inr"] == totals["cost_micro_inr"] and cap["default_cap"]

    everyone = (await client.get("/admin/api/costs", params={"group_by": "business"})).json()
    by_business = {r["key"]: r for r in everyone["rows"]}
    assert by_business[str(other.business_id)]["cost_micro_inr"] == 7_000
    assert by_business[str(other.business_id)]["label"] == "Shop"  # the business's name, not its id

    for bad in ({"from": "2026-10-10", "to": "2026-10-01"}, {"from": "2026-01-01", "to": "2026-10-01"}):
        assert (await client.get("/admin/api/costs", params=bad)).status_code == 422, bad


async def test_raising_a_cap_is_what_metering_reads_next(db: Database, client: httpx.AsyncClient) -> None:
    shop = await make_shop(db)
    csrf = await login(client)
    path = f"/admin/api/cost-caps/{shop.business_id}"
    resp = await client.patch(path, json={"daily_cap_inr": 45}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200, resp.text
    assert resp.json()["daily_cap_micro_inr"] == 45_000_000 and resp.json()["default_cap"] is False
    [row] = await q(db, "SELECT daily_cap_micro_inr FROM cost_caps WHERE business_id = :b", b=shop.business_id)
    assert row.daily_cap_micro_inr == 45_000_000  # the row metering's cap check reads on every call
    for bad in (0.5, 20_000):
        r = await client.patch(path, json={"daily_cap_inr": bad}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 422, bad
    assert (await client.patch(path, json={"daily_cap_inr": 45})).status_code == 403  # no CSRF token


async def test_a_baseline_is_accepted_only_with_a_reason_and_never_rewrites_the_file(
    db: Database, client: httpx.AsyncClient
) -> None:
    result = benchmark.BenchmarkResult(
        set_version=benchmark.set_version(),
        measures={"chat_reply_ms": benchmark.Measure(median=5000, p95=9000, n=10)},
    )
    first = await benchmark._store(result, started_at=utcnow())
    second = await benchmark._store(result, started_at=utcnow())
    file_before = benchmark.BASELINE_FILE.read_text(encoding="utf-8") if benchmark.BASELINE_FILE.exists() else None
    made: list[UUID] = [first, second]
    try:
        csrf = await login(client)
        headers = {"X-CSRF-Token": csrf}
        listed = {r["id"] for r in (await client.get("/admin/api/benchmarks")).json()["runs"]}
        assert {str(first), str(second)} <= listed

        accept = f"/admin/api/benchmarks/{first}/accept"
        assert (await client.post(accept, json={"with": [str(second)]}, headers=headers)).status_code == 422
        short = {"reason": "ok", "with": [str(second)]}
        assert (await client.post(accept, json=short, headers=headers)).status_code == 422
        good = {"reason": "Speed held; cost up 4% for the new purposes, accepted by Taha", "with": [str(second)]}
        resp = await client.post(accept, json=good, headers=headers)
        assert resp.status_code == 200, resp.text
        baseline = UUID(resp.json()["id"])
        made.append(baseline)
        assert resp.json()["results"]["runs"] == 2 and resp.json()["results"]["accepted_by"] == "admin"

        page = (await client.get("/admin/api/benchmarks")).json()
        assert page["baseline"]["id"] == str(baseline) and page["baseline"]["source"] == "database"
        assert page["baseline"]["results"]["accepted_reason"] == good["reason"]
        after = benchmark.BASELINE_FILE.read_text(encoding="utf-8") if benchmark.BASELINE_FILE.exists() else None
        assert after == file_before  # the console never writes the file in git

        again = await client.post(f"/admin/api/benchmarks/{baseline}/accept", json=good, headers=headers)
        assert again.status_code == 422  # a baseline is not combined again
        unknown = await client.post(f"/admin/api/benchmarks/{new_id()}/accept", json=good, headers=headers)
        assert unknown.status_code == 422
    finally:  # the session's database is shared: leave no baseline for other tests
        async with db.service_session(ServiceReason.COSTS) as s:
            await s.execute(text("DELETE FROM benchmark_runs WHERE id = ANY(:ids)"), {"ids": made})


async def test_the_access_logs_show_operator_views_and_vault_reads_and_the_reviewer_only_its_shops(
    db: Database, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflexo.common.config import get_settings

    shown, hidden = await make_shop(db), await make_shop(db)
    access.forget()
    await login(client)
    assert (await client.get("/admin/api/customers", params={"business_id": str(shown.business_id)})).status_code == 200
    await access.log_access(hidden.business_id, "admin", "customer", None, "console GET /admin/api/customers")
    credential = await vault.put(shown.business_id, "telegram_bot_token", {"token": "t"}, "audit test")
    await vault.get(credential, purpose="send:telegram", accessor="gateway.connections")

    views = (await client.get("/admin/api/access-log", params={"kind": "operator"})).json()["items"]
    mine = [v for v in views if v["business_id"] == str(shown.business_id)]
    assert mine and mine[0]["who"] == "admin" and mine[0]["what"] == "customers"
    reads = (await client.get("/admin/api/access-log", params={"kind": "credential"})).json()["items"]
    [read] = [r for r in reads if r["object_id"] == str(credential)]
    assert (read["who"], read["what"], read["detail"], read["ok"]) == (
        "gateway.connections", "telegram_bot_token", "send:telegram", True
    )  # fmt: skip

    monkeypatch.setenv("REFLEXO_REVIEWER_USER", "reviewer")
    monkeypatch.setenv("REFLEXO_REVIEWER_PASSWORD", "public-review-pass")
    monkeypatch.setenv("REFLEXO_REVIEWER_BUSINESSES", f"shop-{shown.business_id}")
    get_settings.cache_clear()
    try:
        csrf = await login(client, "reviewer", "public-review-pass")
        seen = (await client.get("/admin/api/access-log", params={"kind": "operator"})).json()["items"]
        assert seen and {v["business_id"] for v in seen} == {str(shown.business_id)}
        caps = (await client.get("/admin/api/costs")).json()["caps"]
        assert {c["business_id"] for c in caps} <= {str(shown.business_id)}
        hidden_costs = await client.get("/admin/api/costs", params={"business_id": str(hidden.business_id)})
        assert hidden_costs.status_code == 404
        raise_cap = await client.patch(f"/admin/api/cost-caps/{shown.business_id}", json={"daily_cap_inr": 99},
                                       headers={"X-CSRF-Token": csrf})  # fmt: skip
        assert raise_cap.status_code == 403 and raise_cap.json()["code"] == "READ_ONLY"
    finally:
        get_settings.cache_clear()
