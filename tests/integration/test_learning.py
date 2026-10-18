"""The learning employee (ADR-015, POC_10): capabilities decided by the owner's playbook, proposals the owner decides,
decisions carried out exactly as proposed and passed on to the customer. No business flow is coded: cancellation is
just the first capability."""

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
import reflexo.owner.api as owner_api
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.knowledge.playbook import Condition
from reflexo.pipeline import api as pipeline_api
from tests import fake_writer
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
    return fake_writer.install(agent.FakeProvider())  # owner messages are written by purpose (BETA_13 §6)


def prompts_for(fake: agent.FakeProvider, purpose: str) -> list[str]:
    """What the `messages` role was given for one purpose: the facts and the context of the owner's message."""
    return [
        m.content or ""
        for a in fake.calls
        if a.model == fake_writer.MESSAGES_MODEL
        for m in a.messages
        if m.role == "user" and "# PURPOSE" in (m.content or "") and purpose in (m.content or "")
    ]


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
    "done": "Done. Your order is cancelled.",
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
    fyi = [str(m) for m in await owner_messages(db, shop) if order.order_number in str(m)]
    assert fyi and "Cancelling an order" in fyi[0] and "apv:" not in fyi[0]  # an FYI, not a question


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
    brief = next(str(m) for m in await owner_messages(db, shop) if f"apv:{proposal.id}:a" in str(m))
    assert "Cancelling an order" in brief and order.order_number in brief and proposal.short_code in brief
    assert any("I suggest" in p for p in prompts_for(fake, "only the owner can allow"))

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
    assert any(
        "OPEN WITH THIS CUSTOMER" in s and "no answer yet" in s for s in seen
    )  # the second turn knows it is still pending


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
    assert any("24 ghante pehle tak cancel free hai" in str(m) for m in await owner_messages(db, shop))  # the rule
    assert not await q(db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid)


def learning(script_for_desk: Any, conditions: list[dict[str, Any]]) -> Any:
    """One fake model: the builder's generalisation prompt gets the conditions, everything else the desk script."""

    def script(a: agent.Attempt) -> agent.FakeReply:
        if any("Propose the general rule" in (m.content or "") for m in a.messages):
            return agent.FakeReply(text=json.dumps({"conditions": conditions}))
        return script_for_desk(a)

    return script


async def test_one_decision_teaches_the_rule_and_undo_forgets_it(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa1, wa2, wa3 = wa_number(), wa_number(), wa_number()
    order1, _ = await placed_order(registry, db, fake, shop, wa1)
    order2, conv2 = await placed_order(registry, db, fake, shop, wa2)
    order3, _ = await placed_order(registry, db, fake, shop, wa3)
    learned = [
        {"field": "order.hours_until_delivery", "op": "gte", "value": 24},
        {"field": "order.status", "op": "is", "value": "delivered"},  # false for this case → dropped
    ]
    fake.script(PRIMARY, learning(asks_to_cancel(order1.order_id, SAY), learned))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa1, "cancel karo"))
    [proposal] = await q(
        db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")

    offer = next(str(m) for m in await owner_messages(db, shop) if f"lrn:{proposal.id}:g" in str(m))
    readback = "Cancelling an order: I do it myself when hours left until delivery is at least 24"
    assert readback in offer
    assert "status is delivered" not in offer  # the condition that did not hold was dropped
    await owner_says(registry, db, shop, callback=f"lrn:{proposal.id}:g")
    assert sum(readback in str(m) for m in await owner_messages(db, shop)) == 2  # the offer, then "saved"
    [rule] = await q(
        db, "SELECT effect, conditions, source, source_ref FROM playbook_rules WHERE business_id = :b", b=shop.bid
    )
    assert (rule.effect, rule.source, rule.source_ref) == ("allow", "owner_decision", proposal.id)
    assert rule.conditions == [{"field": "order.hours_until_delivery", "op": "gte", "value": 24.0}]

    fake.script(PRIMARY, asks_to_cancel(order2.order_id, SAY))  # another customer: no question this time
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa2, "please cancel"))
    assert await status_of(db, order2.order_id) == "cancelled"
    assert (await outbound_texts(db, conv2))[-1] == SAY["done"]
    assert (
        len(await q(db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid))
        == 1
    )
    assert any("Learned from your decision" in str(m) for m in await owner_messages(db, shop))  # the rule's words

    await owner_says(registry, db, shop, "/undo")  # the owner takes the rule back → asking again
    fake.script(PRIMARY, asks_to_cancel(order3.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa3, "cancel"))
    assert await status_of(db, order3.order_id) == "confirmed"
    assert (
        len(await q(db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid))
        == 2
    )


async def test_just_this_once_learns_nothing(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, _ = await placed_order(registry, db, fake, shop, wa)
    fake.script(PRIMARY, learning(asks_to_cancel(order.order_id, SAY), []))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel karo"))
    [proposal] = await q(
        db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")
    await owner_says(registry, db, shop, callback=f"lrn:{proposal.id}:o")
    await owner_says(registry, db, shop, callback=f"lrn:{proposal.id}:g")  # a late second tap changes nothing
    assert not await q(db, "SELECT id FROM playbook_rules WHERE business_id = :b", b=shop.bid)
    texts = [str(m) for m in await owner_messages(db, shop)]
    assert any("ask you again next time" in t for t in texts) and any("Already saved" in t for t in texts)


# ---------------------------------------------------------------- the owner teaches / acts in words


async def test_the_owner_teaches_a_rule_in_words(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    from tests.integration.test_owner_desk import owner_turn, read_back_reply, steps

    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    fake.script(
        PRIMARY,
        steps(
            call(
                "playbook_change",
                capability="order.cancel",
                effect="allow",
                conditions=[{"field": "order.hours_until_delivery", "op": "gte", "value": 24}],
                says="cancellation 24 ghante pehle tak free hai",
            ),
            read_back_reply,
        ),
    )
    await owner_turn(registry, db, shop, "cancellation 24 ghante pehle tak free hai")
    read_back = str((await owner_messages(db, shop))[-1])
    assert "➕ Cancelling an order: I do it myself when hours left until delivery is at least 24" in read_back
    assert not await knowledge.playbook.rules(shop.bid)  # nothing changes before YES
    code = re.search(r"YES (\d{4})", read_back).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    [rule] = await knowledge.playbook.rules(shop.bid)
    assert rule.source == "owner_instruction" and rule.effect == "allow"

    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel please"))
    assert await status_of(db, order.order_id) == "cancelled"
    assert (await outbound_texts(db, conv))[-1] == SAY["done"]


async def test_the_owner_acts_on_an_order_and_the_customer_is_told(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    from tests.integration.test_owner_desk import owner_turn, read_back_reply, steps

    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    fake.script(
        PRIMARY,
        steps(
            call("order_action", order_number=order.order_number, action="cancel", reason="oven toot gaya"),
            read_back_reply,
        ),
    )
    await owner_turn(registry, db, shop, f"{order.order_number} cancel kar do, oven toot gaya")
    read_back = str((await owner_messages(db, shop))[-1])
    assert f"Cancelling an order: {order.order_number}" in read_back
    code = re.search(r"YES (\d{4})", read_back).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    [row] = await q(db, "SELECT status, cancelled_by FROM orders WHERE id = :o", o=order.order_id)
    assert (str(row.status), row.cancelled_by) == ("cancelled", "owner")
    assert not any("lrn:" in str(m) for m in await owner_messages(db, shop))  # nothing to learn from this

    relay = await claim_pending(registry, db, conv)
    seen: list[str] = []

    def relay_reply(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return reply("send", "Sorry, the shop had to cancel your order.", topic="other")

    fake.script(PRIMARY, relay_reply)
    await registry.require(TurnRunnerPort).run_turn(relay)  # type: ignore[type-abstract]
    assert "The shop itself did this" in seen[0] and "oven toot gaya" in seen[0]


# ---------------------------------------------------------------- order.change: the same loop, no new flow


def asks_to_change(order_id: UUID, changes: dict[str, str], say: dict[str, str]) -> Any:
    def script(a: agent.Attempt) -> agent.FakeReply:
        results = tool_results(a)
        if not results:
            return call(
                "order_change",
                order_id=str(order_id),
                changes=changes,
                reason="I'll be out of town",
                note_for_owner="Taha wants to move the delivery by a day. I suggest yes.",
            )
        data = results[-1].get("data", {})
        outcome = data.get("outcome", f"error:{results[-1].get('error')}")
        text_ = say.get(outcome, f"[{outcome}: {data.get('reason', '')}]")
        return reply("send", text_, topic="other")

    return script


async def test_a_date_change_follows_the_same_learning_loop(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    [d] = await q(db, "SELECT delivery_date + 1 AS d FROM orders WHERE id = :o", o=order.order_id)
    new_day = d.d.isoformat()
    say = {"done": "Done. Moved.", "asked_owner": "I've asked the shop.", "not_possible": "That's not possible."}
    fake.script(PRIMARY, learning(asks_to_change(order.order_id, {"delivery_date": new_day}, say), []))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "ek din aage kar do"))
    assert (await outbound_texts(db, conv))[-1] == say["asked_owner"]
    [proposal] = await q(
        db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    brief = next(str(m) for m in await owner_messages(db, shop) if "apv:" in str(m))
    assert "Changing a placed order" in brief and new_day in brief

    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")
    [row] = await q(db, "SELECT delivery_date FROM orders WHERE id = :o", o=order.order_id)
    assert row.delivery_date.isoformat() == new_day
    assert any("lrn:" in str(m) for m in await owner_messages(db, shop))  # it offers to learn here too


async def test_a_full_day_or_a_paid_total_cannot_change(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    import reflexo.automations.api as automations

    automations.register(registry)
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    await knowledge.playbook.add_rule(
        shop.bid, capability="order.change", effect="allow", conditions=[], says="date change theek hai",
        source="owner_instruction",
    )  # fmt: skip
    installed = await automations.install(shop.bid, automations.demo.DAILY_CAKE_LIMIT, description_nl="8 cake roz")
    [d] = await q(db, "SELECT delivery_date + 1 AS d FROM orders WHERE id = :o", o=order.order_id)
    full_day = d.d.isoformat()
    async with db.service_session(ServiceReason.ADMIN) as s:  # that day already has 8 cakes
        await s.execute(
            text("""INSERT INTO automation_counters (automation_id, business_id, counter_key, value, limit_value)
                    VALUES (:a, :b, :k, 8, 8)"""),
            {"a": installed.automation_id, "b": shop.bid, "k": f"cakes:{full_day}"},
        )
    say = {"done": "Done. Moved.", "not_possible": "That day is full, sorry."}
    fake.script(PRIMARY, asks_to_change(order.order_id, {"delivery_date": full_day}, say))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "ek din aage"))
    assert (await outbound_texts(db, conv))[-1] == say["not_possible"]
    [row] = await q(db, "SELECT delivery_date FROM orders WHERE id = :o", o=order.order_id)
    assert row.delivery_date.isoformat() != full_day  # unchanged

    async with db.service_session(ServiceReason.ADMIN) as s:  # paid: a change that moves the total is refused
        await s.execute(text("UPDATE orders SET payment_status = 'paid' WHERE id = :o"), {"o": order.order_id})
    [before] = await q(db, "SELECT total_paise, address FROM orders WHERE id = :o", o=order.order_id)
    assert before.address is None  # no address yet → no delivery fee; Kothrud adds ₹50 (under ₹1,500)
    with pytest.raises(commerce.CommerceError) as err:
        await commerce.change_order(shop.bid, order.order_id, {"address": "Kothrud 411038"}, by="customer")
    assert err.value.code == "NOT_CHANGEABLE" and "total would become" in str(err.value)
    [after] = await q(db, "SELECT total_paise, address FROM orders WHERE id = :o", o=order.order_id)
    assert (after.total_paise, after.address) == (before.total_paise, None)  # rolled back, nothing half-changed


# ---------------------------------------------------------------- refund.request: money is proposed by default


async def test_a_refund_is_proposed_and_executed_once_approved(
    registry: Registry, db: Database, fake: agent.FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    refunds: list[tuple[str, int]] = []

    async def refund_payment(payment_id: str, amount_paise: int, notes: dict[str, str]) -> dict[str, Any]:
        refunds.append((payment_id, amount_paise))
        return {"id": "rfnd_test", "amount": amount_paise, "status": "processed"}

    monkeypatch.setattr(integrations.razorpay(), "refund_payment", refund_payment, raising=False)
    shop = await bakery(db)
    wa = wa_number()
    order, conv = await placed_order(registry, db, fake, shop, wa)
    async with db.service_session(ServiceReason.ADMIN) as s:  # paid online
        await s.execute(
            text("""INSERT INTO payment_links (id, business_id, order_id, customer_id, provider, amount_paise, status,
                                               expires_at, paid_at, provider_link_id, short_url, idempotency_key,
                                               provider_payment_id)
                    SELECT :id, business_id, id, customer_id, 'razorpay', total_paise, 'paid', now() + interval '1 day',
                           now(), 'plink_t', 'https://rzp.io/t', :k, 'pay_t' FROM orders WHERE id = :o"""),
            {"id": new_id(), "k": f"t-{new_id().hex}", "o": order.order_id},
        )
        await s.execute(text("UPDATE orders SET payment_status = 'paid' WHERE id = :o"), {"o": order.order_id})

    def script(a: agent.Attempt) -> agent.FakeReply:
        results = tool_results(a)
        if not results:
            return call(
                "refund_request", order_id=str(order.order_id), reason="cake kharab tha",
                note_for_owner="Taha says the cake was bad; paid online. Suggest a refund.",
            )  # fmt: skip
        return reply("send", "Maine shop se pooch liya hai.", topic="returns_refunds_cancellation")

    fake.script(PRIMARY, learning(script, []))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "refund chahiye"))
    assert (await outbound_texts(db, conv))[-1] == "Maine shop se pooch liya hai."  # not forced into a handoff
    assert refunds == []  # money: nothing happens before the owner decides
    [proposal] = await q(
        db, "SELECT id FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=shop.bid
    )
    await owner_says(registry, db, shop, callback=f"apv:{proposal.id}:a")
    assert refunds == [("pay_t", order.total_paise)]
    [row] = await q(db, "SELECT payment_status FROM orders WHERE id = :o", o=order.order_id)
    assert str(row.payment_status) == "refunded"
    assert any("refund rfnd_test started" in str(m) for m in await owner_messages(db, shop))
