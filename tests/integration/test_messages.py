"""Messages written by purpose and checked (BETA_13 §6): the check, one repair round, the fallbacks (CHK-MSG-2), and a
model outage end to end (CHK-MSG-3)."""

from __future__ import annotations

import json
from typing import Any

import pytest

import reflexo.agent.api as agent
from reflexo.agent import messages
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database
from reflexo.common.ports import Registry
from tests import fake_writer
from tests.integration import test_ask_owner
from tests.integration.test_ask_owner import bakery, owner_messages, owner_says, wa_number
from tests.integration.test_customer_desk import FALLBACK, PRIMARY, outbound_texts, turn_for

pytestmark = pytest.mark.integration
fake, registry = test_ask_owner.fake, test_ask_owner.registry  # the same fixtures: real owner channel and notifier
MODEL = fake_writer.MESSAGES_MODEL
FACTS: dict[str, Any] = {
    "customer.name": "Rahul",
    "action": "Cancelling an order",
    "order.number": "MB-1042",
    "order.total": messages.Amount(paise=65000),
    "reason": "plans changed",
    "code": "4821",
}
BUTTONS = [{"id": "approve", "label": "Yes, cancel"}, {"id": "reject", "label": "No"}]


def written(text: str, buttons: list[dict[str, str]] = BUTTONS) -> agent.FakeReply:
    return agent.FakeReply(text=json.dumps({"text": text, "buttons": buttons}))


GOOD = "Rahul wants Cancelling an order for MB-1042 (₹650). Reason: plans changed. Reply YES 4821 or NO 4821."


async def compose(shop: Any) -> messages.ComposedMessage:
    return await messages.compose(
        "owner.proposal",
        business_id=shop.bid,
        recipient=messages.RecipientRef(kind="owner"),
        facts=FACTS,
        context={"assistant_note": "Placed today, not prepared yet."},
    )


@pytest.mark.check("CHK-MSG-2")
async def test_a_wrong_amount_gets_one_repair_round(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    fake.scripts.pop(MODEL, None)
    fake.queue(MODEL, written(GOOD.replace("₹650", "₹700")), written(GOOD))
    m = await compose(shop)
    assert (m.text, m.fallback_used, m.check.passed) == (GOOD, False, True)
    calls = [c for c in fake.calls if c.model == MODEL]
    assert len(calls) == 2
    repair = calls[1].messages[-1].content or ""
    assert "₹700" in repair and "NOT sent" in repair  # the exact failure goes back to the writer


@pytest.mark.check("CHK-MSG-2")
async def test_a_second_failure_sends_the_fact_card_never_the_unchecked_text(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    fake.scripts.pop(MODEL, None)
    fake.queue(MODEL, written(GOOD.replace("₹650", "₹700")), written(GOOD, buttons=BUTTONS[:1]))  # a button lost
    m = await compose(shop)
    assert m.fallback_used and [c.name for c in m.check.failures] == ["buttons"]
    assert m.text.splitlines()[0] == "Customer request"  # the fact card: labelled facts, no sentence
    assert "Order: MB-1042" in m.text and "Total: ₹650" in m.text and "Code: 4821" in m.text
    assert [(b.id, b.label) for b in m.buttons] == [("approve", "✅ Yes"), ("reject", "❌ No")]
    assert "₹700" not in m.text and len([c for c in fake.calls if c.model == MODEL]) == 2


def test_the_check_requires_facts_links_and_buttons_and_forbids_invented_values() -> None:
    p = messages.purpose("owner.proposal")
    ok = messages.Written(text=GOOD, buttons=[messages.ButtonLabel(**b) for b in BUTTONS])
    assert messages.check_message(p, ok, FACTS).passed
    cases = {
        "facts": GOOD.replace("MB-1042", "MB-1043"),
        "nothing_invented": GOOD + " Or call 9876543210, or pay at https://evil.example/pay, 10% off.",
        "length": GOOD + " x" * 400,
    }
    for failing, text_ in cases.items():
        result = messages.check_message(p, ok.model_copy(update={"text": text_}), FACTS)
        assert failing in [c.name for c in result.failures], failing
    too_long = messages.Written(text=GOOD, buttons=[messages.ButtonLabel(id="approve", label="x" * 30)])
    assert "buttons" in [c.name for c in messages.check_message(p, too_long, FACTS).failures]


@pytest.mark.check("CHK-MSG-3")
async def test_with_the_model_down_owners_get_fact_cards_and_customers_one_holding_line(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    down = agent.FakeReply(error=agent.ModelError("PROVIDER_UNAVAILABLE", "503"))
    for model in {PRIMARY, FALLBACK, MODEL}:
        fake.reply(model, down)
    wa = wa_number()
    for body in ("hello", "koi hai?", "please reply"):
        handle = await turn_for(registry, db, shop.pnid, wa, body)
        await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    assert len(await outbound_texts(db, handle.ref.conversation_id)) == 1  # one holding line today, not three

    notes = await owner_messages(db, shop)
    first, *followups = notes
    assert first["text"].splitlines()[0] == "Question" and "Code: " in first["text"]
    assert followups and all(n["text"].splitlines()[0] == "Customer wrote again" for n in followups)
    callbacks = [b[0]["callback_data"] for b in first["reply_markup"]["inline_keyboard"]]
    later = next(c for c in callbacks if c.endswith(":later"))
    await owner_says(registry, db, shop, callback=later.removeprefix("o:"))  # the buttons still work
    assert "Reply" in (await owner_messages(db, shop))[-1]["text"]


async def test_the_owner_is_told_once_when_ai_use_reaches_the_alert_share(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    from sqlalchemy import text

    from reflexo.agent import metering
    from reflexo.common.costs import Usage
    from reflexo.common.db import ServiceReason
    from reflexo.common.enums import ModelCallKind
    from reflexo.common.llm import CallCtx

    shop = await bakery(db)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO cost_caps (business_id, daily_cap_micro_inr) VALUES (:b, 50000)
                    ON CONFLICT (business_id) DO UPDATE SET daily_cap_micro_inr = 50000, alerted_on = NULL"""),
            {"b": shop.bid},
        )
    ctx = CallCtx(business_id=shop.bid, purpose="customer_turn")
    for _ in range(4):  # about 17,100 micro-rupees each: the 80% line (40,000) is crossed on the third
        await metering.record(ctx, kind=ModelCallKind.CHAT, provider="gemini", model="gemini-2.5-flash-lite",
                              usage=Usage(input_tokens=1000, output_tokens=200), duration_ms=10)  # fmt: skip
    from tests.integration.test_ask_owner import q
    from tests.integration.test_commerce import run_jobs

    assert await run_jobs(registry, metering.CAP_WARNING_QUEUE) >= 1  # through the queue, as the worker runs it
    note = (await owner_messages(db, shop))[-1]["text"]
    assert "daily_cap: ₹0.05" in note and "share: 102.6%" in note  # from the records: 51,300 of 50,000 micro-rupees
    assert len(await q(db, "SELECT id FROM jobs WHERE queue = :q AND business_id = :b",
                       q=metering.CAP_WARNING_QUEUE, b=shop.bid)) == 1  # once that day  # fmt: skip
