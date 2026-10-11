"""Capacity guard on a real database (CHK-F08-2, CHK-F08-3; POC_06 §6): exactly the limit, never more."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import pytest

import reflexo.agent.api as agent
import reflexo.automations.api as automations
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
from reflexo.common.config import get_settings
from reflexo.common.contracts import OrderGuardPort
from reflexo.common.db import Database
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_commerce import FakeRazorpay, ready_order, shop_with_customer
from tests.integration.test_customer_desk import make_bakery

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_GUARD_TIMEOUT_MS", "10000")  # 12 concurrent checks on laptop Docker
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def rzp(monkeypatch: pytest.MonkeyPatch) -> FakeRazorpay:
    client = FakeRazorpay()
    monkeypatch.setattr(integrations, "_client", client)
    return client


@pytest.fixture
def registry(rzp: FakeRazorpay) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=agent.FakeProvider())
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    owner_api.register(reg)
    automations.register(reg)  # the real OrderGuardPort
    return reg


def guard(registry: Registry) -> Any:
    return registry.require(OrderGuardPort)  # type: ignore[type-abstract]


async def cake(bid: UUID) -> Any:
    return next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)


def order(card: Any, day: str, qty: int = 1) -> dict[str, Any]:
    return {
        "order_id": str(new_id()),
        "order_number": f"MB-{new_id().int % 10000}",
        "lines": [{"offering_id": str(card.offering_id), "name": card.name, "quantity": qty, "adjustment": None}],
        "total_paise": 95000 * qty,
        "delivery_date": day,
        "customer_id": None,
    }


async def test_ninth_cake_is_denied_with_alternatives_and_release_frees_a_slot(
    registry: Registry, db: Database
) -> None:
    bid, _ = await make_bakery(db)
    await automations.install(bid, automations.demo.DAILY_CAKE_LIMIT, description_nl="max 8 cakes per day")
    truffle = await cake(bid)
    day = (date.today() + timedelta(days=3)).isoformat()
    booked = [order(truffle, day) for _ in range(8)]
    for o in booked:
        assert (await guard(registry).check(bid, o)).allowed
    assert (await guard(registry).check(bid, booked[0])).allowed  # a retry of the same order does not count twice

    ninth = await guard(registry).check(bid, order(truffle, day))
    assert not ninth.allowed and ninth.code == "CAPACITY_FULL"
    assert "ke liye cake orders full ho gaye hain" in (ninth.reason or "")
    assert len(ninth.alternatives) >= 2 and day not in ninth.alternatives

    await guard(registry).release(bid, UUID(booked[0]["order_id"]), "order_cancelled")
    assert (await guard(registry).check(bid, order(truffle, day))).allowed  # the freed slot is usable

    two = await guard(registry).check(bid, order(truffle, (date.today() + timedelta(days=4)).isoformat(), qty=9))
    assert not two.allowed  # 9 cakes in one order never fit a limit of 8


async def test_twelve_parallel_confirms_for_one_date_exactly_eight_succeed(registry: Registry, db: Database) -> None:
    bid, _ = await make_bakery(db)
    await automations.install(bid, automations.demo.DAILY_CAKE_LIMIT, description_nl="max 8 cakes per day")
    truffle = await cake(bid)
    day = (date.today() + timedelta(days=5)).isoformat()
    results = await asyncio.gather(*(guard(registry).check(bid, order(truffle, day)) for _ in range(12)))
    assert sum(r.allowed for r in results) == 8
    assert {r.code for r in results if not r.allowed} == {"CAPACITY_FULL"}


async def test_guard_errors_fail_closed(registry: Registry, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    bid, _ = await make_bakery(db)
    await automations.install(bid, automations.demo.DAILY_CAKE_LIMIT, description_nl="max 8 cakes per day")

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    import reflexo.automations.guard as guard_module

    monkeypatch.setattr(guard_module, "order_variables", broken)
    result = await guard(registry).check(bid, order(await cake(bid), date.today().isoformat()))
    assert (result.allowed, result.code) == (False, "GUARD_ERROR")


async def test_no_guard_automations_allow_everything(registry: Registry, db: Database) -> None:
    bid, _ = await make_bakery(db)
    assert (await guard(registry).check(bid, order(await cake(bid), date.today().isoformat()))).allowed


async def test_commerce_confirm_is_declined_when_full(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    one_per_day = copy.deepcopy(automations.demo.DAILY_CAKE_LIMIT)
    one_per_day["steps"][0]["limit"] = 1
    await automations.install(shop["bid"], one_per_day, description_nl="1 cake per day")
    first = await ready_order(shop)
    assert (await commerce.confirm_order(shop["bid"], first.order_id, "razorpay_link", idem="g1")).status == (
        "awaiting_payment"
    )
    other = await shop_with_customer_same_business(db, shop)
    second = await ready_order(other)
    declined = await commerce.confirm_order(shop["bid"], second.order_id, "razorpay_link", idem="g2")
    assert (declined.status, declined.decline_code) == ("declined", "CAPACITY_FULL")
    assert declined.alternatives and len(rzp.created) == 1  # no link for the declined order


async def shop_with_customer_same_business(db: Database, shop: dict[str, Any]) -> dict[str, Any]:
    """A second customer of the same shop (same cards) with their own conversation."""
    from sqlalchemy import text

    from reflexo.common.db import ServiceReason

    cust, conv = new_id(), new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO customers (id, business_id, display_name) VALUES (:id, :b, 'Priya')"),
            {"id": cust, "b": shop["bid"]},
        )
        wa = (
            await s.execute(
                text("SELECT id FROM channel_connections WHERE business_id = :b AND channel = 'whatsapp'"),
                {"b": shop["bid"]},
            )
        ).scalar_one()
        await s.execute(
            text("""INSERT INTO conversations (id, business_id, channel, channel_connection_id, participant_role,
                                               customer_id, thread_key, last_inbound_at)
                    VALUES (:id, :b, 'whatsapp', :c, 'customer', :cu, :tk, now())"""),
            {"id": conv, "b": shop["bid"], "c": wa, "cu": cust, "tk": f"9198{new_id().int % 10**8:08d}"},
        )
    return {**shop, "cust": cust, "conv": conv}
