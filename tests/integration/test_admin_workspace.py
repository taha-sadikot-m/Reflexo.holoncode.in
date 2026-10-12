"""Admin workspace pages (POC_07 §2, WP-13a): the learning loop seen from the admin side — the order, the proposal,
the rule with its read-back and source decision, and undo through the same change-set revert as `/undo`; plus
automations pause/resume and business isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

import reflexo.agent.api as agent
import reflexo.automations.api as automations
from reflexo.api.main import app
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry
from tests.integration.test_admin_api import login
from tests.integration.test_ask_owner import bakery, owner_says, q, wa_number
from tests.integration.test_customer_desk import PRIMARY, turn_for
from tests.integration.test_learning import (  # noqa: F401  (fixtures)
    SAY,
    _generous_speculative_budget,
    asks_to_cancel,
    fake,
    learning,
    placed_order,
    registry,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_the_learning_loop_in_the_admin_pages(
    registry: Registry, db: Database, fake: agent.FakeProvider, client: httpx.AsyncClient
) -> None:
    shop = await bakery(db)
    other = await bakery(db)
    wa = wa_number()
    order, _ = await placed_order(registry, db, fake, shop, wa)
    learned = [{"field": "order.hours_until_delivery", "op": "gte", "value": 24}]
    fake.script(PRIMARY, learning(asks_to_cancel(order.order_id, SAY), learned))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel karo"))
    [proposal] = await q(
        db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")
    await owner_says(registry, db, shop, callback=f"lrn:{proposal.id}:g")

    csrf = await login(client)
    h = {"X-CSRF-Token": csrf}
    p = {"business_id": str(shop.bid)}

    orders = (await client.get("/admin/api/orders", params=p)).json()
    [row] = [o for o in orders["items"] if o["id"] == str(order.order_id)]
    assert (row["status"], row["cancelled_by"]) == ("cancelled", "customer") and "Truffle" in row["items"]
    assert orders["counts"]["cancelled"] >= 1
    assert (await client.get("/admin/api/orders", params={**p, "status": "nope"})).status_code == 422
    detail = (await client.get(f"/admin/api/orders/{order.order_id}", params=p)).json()
    assert "order.cancelled" in [e["type"] for e in detail["timeline"]]
    assert [(x["capability"], x["status"], x["outcome"]["learned"]) for x in detail["proposals"]] == [
        ("order.cancel", "approved", "g")
    ]
    assert (await client.get(f"/admin/api/orders/{new_id()}", params=p)).status_code == 404

    book = (await client.get("/admin/api/playbook", params=p)).json()
    [rule] = book["rules"]
    assert rule["readback"] == "Cancelling an order: I do it myself when hours left until delivery is at least 24"
    assert rule["source"] == "owner_decision" and rule["source_decision"]["approval_id"] == str(proposal.id)
    assert rule["status"] == "active" and rule["change_set_id"]
    [prop] = book["proposals"]
    assert (prop["label"], prop["done"], prop["learned"]) == ("Cancelling an order", True, "g")

    # undo from the admin page = the owner's /undo: the rule's change set is reverted, the rule archived
    revert = f"/admin/api/catalogue/change-sets/{rule['change_set_id']}/revert"
    assert (await client.post(revert, params=p)).status_code == 403  # CSRF
    assert (await client.post(revert, params=p, headers=h)).status_code == 200
    assert (await client.post(revert, params=p, headers=h)).status_code == 409  # already reverted
    assert (
        await client.post(f"/admin/api/catalogue/change-sets/{new_id()}/revert", params=p, headers=h)
    ).status_code == 404
    [after] = (await client.get("/admin/api/playbook", params=p)).json()["rules"]
    assert after["status"] == "archived" and after["change_set_id"] is None
    sets = (await client.get("/admin/api/catalogue/change-sets", params=p)).json()
    assert sets[0]["summary"].startswith("Undo:") and sets[0]["items"][0]["entity"] == "playbook_rule"

    owner = (await client.get("/admin/api/owner", params=p)).json()
    assert any(a["kind"] == "tool_call" for a in owner["approvals"])

    # another business sees none of it
    o = {"business_id": str(other.bid)}
    assert (await client.get("/admin/api/playbook", params=o)).json() == {"rules": [], "proposals": []}
    assert (await client.get(f"/admin/api/orders/{order.order_id}", params=o)).status_code == 404
    assert (await client.post(revert, params=o, headers=h)).status_code == 404


async def test_automations_pause_resume_and_capacity(db: Database, client: httpx.AsyncClient) -> None:
    shop = await bakery(db)
    installed = await automations.install(shop.bid, automations.demo.DAILY_CAKE_LIMIT, description_nl="8 cake roz")
    csrf = await login(client)
    h = {"X-CSRF-Token": csrf}
    p = {"business_id": str(shop.bid)}
    aid = installed.automation_id

    [a] = (await client.get("/admin/api/automations", params=p)).json()
    assert (a["id"], a["status"], a["version"], a["runs_today"]) == (str(aid), "active", 1, 0) and a["readback"]
    detail = (await client.get(f"/admin/api/automations/{aid}", params=p)).json()
    assert detail["definition"]["steps"] and detail["runs"] == []

    assert (await client.post(f"/admin/api/automations/{aid}/pause", params=p, headers=h)).json() == {
        "status": "paused"
    }
    assert aid not in [v.automation_id for v in await automations.active_versions(shop.bid)]  # no longer routed
    assert (await client.post(f"/admin/api/automations/{aid}/resume", params=p, headers=h)).json() == {
        "status": "active"
    }
    assert aid in [v.automation_id for v in await automations.active_versions(shop.bid)]
    assert (await client.post(f"/admin/api/automations/{new_id()}/pause", params=p, headers=h)).status_code == 404
    assert (
        await client.post(f"/admin/api/automation-runs/{new_id()}/skip-wait", params=p, headers=h)
    ).status_code == 404
    assert (await client.get("/admin/api/orders/capacity", params=p)).json() == []
    assert (await client.get("/admin/api/imports", params=p)).json() == []
    assert (await client.get("/admin/api/calls", params=p)).json() == []
