"""The learning employee (ADR-015, POC_10): capabilities decided by the owner's playbook, proposals the owner decides,
decisions carried out exactly as proposed and passed on to the customer. No business flow is coded: cancellation is
just the first capability."""

from __future__ import annotations

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
import reflexo.owner.api as owner_api
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ports import Registry, reset_registry
from reflexo.knowledge.playbook import Condition
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_ask_owner import Bakery, bakery, claim_pending, owner_messages, owner_says, q, wa_number
from tests.integration.test_commerce import FakeRazorpay
from tests.integration.test_customer_desk import PRIMARY, outbound_texts, reply, tool_results, turn_for
from tests.integration.test_order_turns import call, customer_of

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
def registry(fake: agent.FakeProvider, monkeypatch: pytest.MonkeyPatch) -> Registry:
    monkeypatch.setattr(integrations, "_client", FakeRazorpay())
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=fake)
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    owner_api.register(reg)  # real notifier (Telegram rows) + real owner commands (buttons, YES/NO)
    return reg


async def placed_order(registry: Registry, db: Database, fake: agent.FakeProvider, shop: Bakery, wa: str) -> Any:
    """A customer with a confirmed order delivering in 3 days (status set directly: confirm flows are tested
    elsewhere; the capability only reads the order's facts)."""
    fake.script(PRIMARY, lambda a: reply("send", "Namaste!", topic="greeting_smalltalk"))
    first = await turn_for(registry, db, shop.pnid, wa, "hi")
    await registry.require(TurnRunnerPort).run_turn(first)  # type: ignore[type-abstract]
    conv = first.ref.conversation_id
    truffle = next(c for c in await knowledge.search_catalog(shop.bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v for v in truffle.variants if v.name == "1 kg")
    order = await commerce.start_order(
        shop.bid,
        await customer_of(db, conv),
        conv,
        [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg.variant_id)],
    )
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""UPDATE orders SET status = 'confirmed', payment_method = 'cod', payment_status = 'cod_pending',
                           delivery_date = current_date + 3, delivery_slot = '4pm-8pm' WHERE id = :o"""),
            {"o": order.order_id},
        )
    return order, conv


def asks_to_cancel(order_id: UUID, say: dict[str, str]) -> Any:
    """The model asks for the capability, then says whatever the tool's outcome warrants."""

    def script(a: agent.Attempt) -> agent.FakeReply:
        results = tool_results(a)
        if not results:
            return call(
                "order_cancel",
                order_id=str(order_id),
                reason="plans changed",
                note_for_owner="Taha wants to cancel; placed today, not prepared yet. I suggest cancelling.",
            )
        outcome = results[-1].get("data", {}).get("outcome", f"error:{results[-1].get('error')}")
        refs = [{"type": "order", "id": str(order_id)}]
        return reply("send", say.get(outcome, f"[{outcome}]"), topic="order_status", refs=refs)

    return script


SAY = {
    "done": "Done — your order is cancelled.",
    "asked_owner": "I've asked the shop, I'll update you shortly.",
    "declined": "Sorry, the shop can't cancel this one.",
}


async def status_of(db: Database, order_id: UUID) -> str:
    [row] = await q(db, "SELECT status FROM orders WHERE id = :o", o=order_id)
    return str(row.status)


async def test_a_draft_is_cancelled_on_day_one_common_sense(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("UPDATE orders SET status = 'draft' WHERE id = :o"), {"o": order.order_id})
    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel it please"))
    assert await status_of(db, order.order_id) == "cancelled"
    assert (await outbound_texts(db, conv))[-1] == SAY["done"]
    assert not await q(db, "SELECT id FROM approval_requests WHERE business_id = :b", b=shop.bid)
    assert any("Done for the customer" in str(m) for m in await owner_messages(db, shop))  # an FYI, not a question


async def test_uncovered_cancel_is_proposed_and_the_owners_yes_is_carried_out(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(
        await turn_for(registry, db, shop.pnid, wa, "mera order cancel karo")
    )

    assert await status_of(db, order.order_id) == "confirmed"  # nothing happens before the owner decides
    assert (await outbound_texts(db, conv))[-1] == SAY["asked_owner"]
    [proposal] = await q(
        db,
        "SELECT id, short_code, subject FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b",
        b=shop.bid,
    )
    assert proposal.subject["capability"] == "order.cancel" and proposal.subject["reason"] == "plans changed"
    brief = next(str(m) for m in await owner_messages(db, shop) if "wants" in str(m))
    assert "Cancelling an order".lower() in brief.lower() and order.order_number in brief and "I suggest" in brief

    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")
    assert await status_of(db, order.order_id) == "cancelled"
    relay = await claim_pending(registry, db, conv)
    assert relay.ref.hints == {"proposal_id": str(proposal.id)}
    seen: list[str] = []

    def relay_reply(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return reply("send", "The shop has cancelled your order.", topic="other")

    fake.script(PRIMARY, relay_reply)
    await registry.require(TurnRunnerPort).run_turn(relay)  # type: ignore[type-abstract]
    assert "THE SHOP DECIDED" in seen[0] and "agreed and it is done" in seen[0]
    assert (await outbound_texts(db, conv))[-1] == "The shop has cancelled your order."
    assert any("Done ✅" in str(m) for m in await owner_messages(db, shop))


async def test_a_no_keeps_the_order_and_the_customer_is_told(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel karo"))
    [proposal] = await q(
        db, "SELECT short_code FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    await owner_says(registry, db, shop, f"NO {proposal.short_code.strip()}")
    assert await status_of(db, order.order_id) == "confirmed"
    relay = await claim_pending(registry, db, conv)
    seen: list[str] = []

    def relay_reply(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return reply("send", "Sorry, the shop can't cancel it.", topic="other")

    fake.script(PRIMARY, relay_reply)
    await registry.require(TurnRunnerPort).run_turn(relay)  # type: ignore[type-abstract]
    assert "decided not to do it" in seen[0]


async def test_asking_again_while_the_owner_decides_is_one_proposal(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, _ = await placed_order(registry, db, fake, shop, wa)
    seen: list[str] = []

    def script(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return asks_to_cancel(order.order_id, SAY)(a)

    fake.script(PRIMARY, script)
    for body in ("cancel karo", "please cancel it, any update?"):
        await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, body))
    assert (
        len(await q(db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid))
        == 1
    )
    assert any("WAITING ON THE SHOP" in s for s in seen)  # the second turn knows it is still pending


async def test_the_owners_rules_decide_without_asking(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    customer = await customer_of(db, conv)
    await knowledge.playbook.add_rule(
        shop.bid,
        capability="order.cancel",
        effect="allow",
        conditions=[Condition(field="order.hours_until_delivery", op="gte", value=24.0)],
        says="24 ghante pehle tak cancel free hai",
        source="owner_instruction",
    )
    await knowledge.playbook.add_rule(  # this customer only: always ask me (the customer rule wins)
        shop.bid,
        capability="order.cancel",
        effect="deny",
        conditions=[],
        says="Taha ke orders cancel nahi honge",
        source="owner_instruction",
        customer_id=customer,
    )
    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel kar do"))
    assert await status_of(db, order.order_id) == "confirmed"
    assert (await outbound_texts(db, conv))[-1] == SAY["declined"]

    async with db.service_session(ServiceReason.ADMIN) as s:  # owner archives the customer rule → the global one
        await s.execute(text("UPDATE playbook_rules SET status = 'archived' WHERE customer_id = :c"), {"c": customer})
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "ab cancel kar do"))
    assert await status_of(db, order.order_id) == "cancelled"
    assert (await outbound_texts(db, conv))[-1] == SAY["done"]
    assert any("your rule" in str(m) and "24 ghante" in str(m) for m in await owner_messages(db, shop))
    assert not await q(db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid)
