"""Order conversations across turns (live WP-09 regressions, 11 Oct 2026).

"yes" in a new turn must confirm the order the previous turn built (the open order is in the context; a
placeholder id gets the real one back), and a failed turn while an owner question is open must not ask again.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
from reflexo.common.contracts import OwnerNotifyPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_commerce import FakeRazorpay
from tests.integration.test_customer_desk import (
    FALLBACK,
    PRIMARY,
    Notes,
    desk,
    make_bakery,
    outbound_texts,
    reply,
    tool_results,
    turn_for,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _generous_speculative_budget(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from reflexo.common.config import get_settings

    monkeypatch.setenv("REFLEXO_SPECULATIVE_BUDGET_MS", "3000")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def fake() -> agent.FakeProvider:
    return agent.FakeProvider()


@pytest.fixture
def notes() -> Notes:
    return Notes()


@pytest.fixture
def rzp(monkeypatch: pytest.MonkeyPatch) -> FakeRazorpay:
    client = FakeRazorpay()
    monkeypatch.setattr(integrations, "_client", client)
    return client


@pytest.fixture
def registry(fake: agent.FakeProvider, notes: Notes, rzp: FakeRazorpay) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=fake)
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    reg.provide(OwnerNotifyPort, notes)  # type: ignore[type-abstract]
    return reg


def call(name: str, **args: Any) -> agent.FakeReply:
    return agent.FakeReply(tool_calls=[ToolCall(id=f"c{new_id().hex[-6:]}", name=name, args=args)])


async def customer_of(db: Database, conversation_id: UUID) -> UUID:
    async with db.service_session(ServiceReason.ADMIN) as s:
        cid: UUID = (
            await s.execute(text("SELECT customer_id FROM conversations WHERE id = :c"), {"c": conversation_id})
        ).scalar_one()
    return cid


async def test_yes_in_next_turn_confirms_the_open_order(
    registry: Registry, db: Database, fake: agent.FakeProvider, rzp: FakeRazorpay
) -> None:
    bid, pnid = await make_bakery(db)
    wa = f"9198{new_id().int % 10**8:08d}"
    fake.script(PRIMARY, lambda a: reply("send", "Namaste! 1 kg truffle ₹950 hai.", topic="pricing"))
    first = await turn_for(registry, db, pnid, wa, "1 kg chocolate truffle chahiye")
    await desk(registry).run_turn(first)
    conv = first.ref.conversation_id
    truffle = next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v.variant_id for v in truffle.variants if v.name == "1 kg")
    order = await commerce.start_order(
        bid, await customer_of(db, conv), conv, [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg)]
    )

    seen: dict[str, Any] = {}

    def script(a: agent.Attempt) -> agent.FakeReply:
        results = tool_results(a)
        if not results:  # the order from the earlier turn is in the context
            prompt = "\n".join(m.content or "" for m in a.messages)
            seen["block"] = re.search(r"# OPEN ORDER.*\n(\{.*\})", prompt)
            return call("order_confirm", order_id="<order_id>", payment_method="razorpay_link")
        last = results[-1]
        if "error" in last:  # live bug: a placeholder id; the guidance carries the real one
            seen["guidance"] = last["error"]["guidance"]
            real = re.search(r"order_id ([0-9a-f-]{36})", last["error"]["guidance"])
            assert real is not None
            return call("order_confirm", order_id=real.group(1), payment_method="razorpay_link")
        link_id = last["data"]["reference"]["id"]
        return reply(
            "send",
            "Here is your payment link. Your order is confirmed once paid.",
            topic="order_status",
            language="en",
            refs=[{"type": "order", "id": str(order.order_id)}],
            extra_blocks=[{"type": "payment_link", "payment_link_id": link_id}],
        )

    fake.script(PRIMARY, script)
    second = await turn_for(registry, db, pnid, wa, "yes")
    await desk(registry).run_turn(second)

    assert seen["block"] is not None
    block = json.loads(seen["block"].group(1))
    assert (block["order_id"], block["order_number"], block["total"]) == (str(order.order_id), "MB-1001", "₹950")
    assert str(order.order_id) in seen["guidance"] and "MB-1001" in seen["guidance"]
    assert len(rzp.created) == 1 and rzp.created[0]["amount_paise"] == 95000
    texts = await outbound_texts(db, conv)
    assert texts[-1].endswith(rzp.created[0]["url"]) or rzp.created[0]["url"] in " ".join(texts[-2:])
    async with db.service_session(ServiceReason.ADMIN) as s:
        status = (await s.execute(text("SELECT status FROM orders WHERE id = :o"), {"o": order.order_id})).scalar_one()
    assert str(status) == "awaiting_payment"


async def test_order_tools_only_touch_this_customers_open_order(registry: Registry, db: Database) -> None:
    bid, _ = await make_bakery(db)
    truffle = next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v.variant_id for v in truffle.variants if v.name == "1 kg")
    async with db.service_session(ServiceReason.SEED) as s:
        a, b = new_id(), new_id()
        for c in (a, b):
            await s.execute(text("INSERT INTO customers (id, business_id) VALUES (:id, :b)"), {"id": c, "b": bid})
    item = [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg)]
    other = await commerce.start_order(bid, b, None, item)
    mine = await commerce.start_order(bid, a, None, item)
    ctx = agent.ToolContext(
        business_id=bid,
        desk="customer",
        channel="whatsapp",
        conversation_id=None,
        customer_id=a,
        member_id=None,
        turn_id=new_id(),
        step=1,
    )
    res = await agent.run_tool(
        call("order_update", order_id=str(other.order_id), details={"eggless": "yes"}).tool_calls[0],
        ctx,
        {"order_update"},
    )
    assert res["error"]["code"] == "INVALID_ARGS" and str(mine.order_id) in res["error"]["guidance"]


async def test_failed_turn_while_owner_question_open_does_not_ask_again(
    registry: Registry, db: Database, fake: agent.FakeProvider, notes: Notes
) -> None:
    _, pnid = await make_bakery(db)
    wa = f"9198{new_id().int % 10**8:08d}"
    fake.script(
        PRIMARY,
        lambda a: reply(
            "ask_owner", None, topic="catalog_info", owner_question={"question": "Do we make sugar-free cakes?"}
        ),
    )
    first = await turn_for(registry, db, pnid, wa, "sugar free cake milta hai?")
    await desk(registry).run_turn(first)
    kind, body = notes.sent[0]
    assert kind == "question" and "👤" in body and "Do we make sugar-free cakes?" in body
    assert "Recent:" not in body  # a colleague's message, not a transcript dump (POC_10 §8)
    assert ("chat:" + str(first.ref.conversation_id), "💬 Show chat") in notes.buttons[0]

    down = agent.FakeReply(error=agent.ModelError("PROVIDER_UNAVAILABLE", "503"))
    fake.script(PRIMARY, lambda a: down)
    fake.script(FALLBACK, lambda a: down)
    second = await turn_for(registry, db, pnid, wa, "any update?")
    await desk(registry).run_turn(second)

    async with db.service_session(ServiceReason.ADMIN) as s:
        questions = (
            await s.execute(
                text("SELECT count(*) FROM owner_questions WHERE conversation_id = :c"),
                {"c": first.ref.conversation_id},
            )
        ).scalar_one()
    assert questions == 1  # live bug: "any update" became question 3162
    assert notes.sent[1][0] == "question_followup" and "any update?" in notes.sent[1][1]
    assert "still" in (await outbound_texts(db, first.ref.conversation_id))[-1].lower()


async def test_read_back_of_the_open_order_is_grounded_without_listing_it(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    """Live: the read-back stated the draft total but listed only the variant, and the gate refused the total."""
    bid, pnid = await make_bakery(db)
    wa = f"9198{new_id().int % 10**8:08d}"
    fake.script(PRIMARY, lambda a: reply("send", "Namaste! 1 kg truffle ₹950 hai.", topic="pricing"))
    first = await turn_for(registry, db, pnid, wa, "1 kg chocolate truffle chahiye")
    await desk(registry).run_turn(first)
    conv = first.ref.conversation_id
    truffle = next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v for v in truffle.variants if v.name == "1 kg")
    order = await commerce.start_order(
        bid,
        await customer_of(db, conv),
        conv,
        [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg.variant_id, quantity=2)],
    )
    total = order.total_paise // 100
    assert order.total_paise != kg.price_paise  # only the order backs this amount
    text_ = f"Order note kar liya: 2 x truffle 1 kg, total ₹{total:,}. Delivery kab chahiye?"
    fake.script(
        PRIMARY,
        lambda a: reply(
            "send", text_, topic="booking_appointments", refs=[{"type": "variant", "id": str(kg.variant_id)}]
        ),
    )
    second = await turn_for(registry, db, pnid, wa, "haan")
    await desk(registry).run_turn(second)
    assert (await outbound_texts(db, conv))[-1] == text_


async def _set_cod_limit(db: Database, bid: UUID, max_paise: int) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE businesses SET settings = settings || CAST(:r AS jsonb) WHERE id = :b"),
            {"r": json.dumps({"cod_rule": {"enabled": True, "max_total_paise": max_paise}}), "b": bid},
        )


async def _draft_for(registry: Registry, db: Database, fake: agent.FakeProvider, pnid: str, wa: str, qty: int) -> Any:
    bid = await _business_of(db, pnid)
    fake.script(PRIMARY, lambda a: reply("send", "Namaste! 1 kg truffle ₹950 hai.", topic="pricing"))
    first = await turn_for(registry, db, pnid, wa, "1 kg chocolate truffle chahiye")
    await desk(registry).run_turn(first)
    conv = first.ref.conversation_id
    truffle = next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v for v in truffle.variants if v.name == "1 kg")
    item = commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg.variant_id, quantity=qty)
    return await commerce.start_order(bid, await customer_of(db, conv), conv, [item]), conv


async def _business_of(db: Database, pnid: str) -> UUID:
    async with db.service_session(ServiceReason.ADMIN) as s:
        bid: UUID = (
            await s.execute(
                text("SELECT business_id FROM channel_connections WHERE external_account_id = :p"), {"p": pnid}
            )
        ).scalar_one()
    return bid


async def test_decline_reason_amount_reaches_the_customer(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    """Live 12 Oct: COD declined ("COD only for orders up to ₹1,500"); the reply quoting that limit was refused
    twice by the gate (the amount came from a tool result, not a cited record) and the customer got a fallback."""
    bid, pnid = await make_bakery(db)
    await _set_cod_limit(db, bid, 123400)  # an amount found nowhere else in the shop
    wa = f"9198{new_id().int % 10**8:08d}"
    order, conv = await _draft_for(registry, db, fake, pnid, wa, qty=2)
    total = order.total_paise // 100
    answer = f"Cash on delivery isn't available for orders over ₹1,234. Your total is ₹{total:,}. Pay by link?"

    def script(a: agent.Attempt) -> agent.FakeReply:
        results = tool_results(a)
        if not results:
            return call("order_confirm", order_id=str(order.order_id), payment_method="cod")
        assert results[-1]["data"]["status"] == "declined"
        # as live: the order is cited (its total); only the decline's tool result backs the ₹1,234 limit
        return reply("send", answer, topic="payments", refs=[{"type": "order", "id": str(order.order_id)}])

    fake.script(PRIMARY, script)
    second = await turn_for(registry, db, pnid, wa, "haan COD se")
    await desk(registry).run_turn(second)
    assert (await outbound_texts(db, conv))[-1] == answer


async def test_a_price_the_customer_suggested_is_never_backed(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    _, pnid = await make_bakery(db)
    fake.script(PRIMARY, lambda a: reply("send", "Theek hai, ₹500 mein de denge!", topic="pricing", refs=[]))
    turn = await turn_for(registry, db, pnid, f"9198{new_id().int % 10**8:08d}", "truffle ₹500 mein de do")
    await desk(registry).run_turn(turn)
    assert "Theek hai, ₹500 mein de denge!" not in await outbound_texts(db, turn.ref.conversation_id)


async def test_an_abandoned_draft_is_not_continued(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    """Live 12 Oct: yesterday's forgotten draft became today's "open order" and the new cake was added to it."""
    bid, pnid = await make_bakery(db)
    wa = f"9198{new_id().int % 10**8:08d}"
    order, conv = await _draft_for(registry, db, fake, pnid, wa, qty=1)
    customer = await customer_of(db, conv)
    assert (await commerce.open_order(bid, customer)).order_id == order.order_id  # type: ignore[union-attr]
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE orders SET updated_at = now() - interval '13 hours' WHERE id = :o"), {"o": order.order_id}
        )
    assert await commerce.open_order(bid, customer) is None
    seen: list[str] = []

    def script(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return reply("send", "Namaste! Kaunsa cake chahiye?", topic="greeting_smalltalk")

    fake.script(PRIMARY, script)
    await desk(registry).run_turn(await turn_for(registry, db, pnid, wa, "hi, cake order karna hai"))
    assert "# OPEN ORDER" not in seen[0]
