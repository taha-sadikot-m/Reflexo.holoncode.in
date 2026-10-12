"""The learning employee with REAL Gemini (ADR-015): the model must use the action tools for natural requests,
report their outcome honestly, and write the owner a useful note.

Opt-in: `uv run pytest --live tests/live/test_learning_live.py -s`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
from reflexo.agent.model_gateway.providers import GeminiProvider
from reflexo.common.config import Settings
from reflexo.common.contracts import OwnerNotifyPort, TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.knowledge.playbook import Condition
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_customer_desk import Notes, make_bakery, outbound_texts, turn_for
from tests.integration.test_order_turns import customer_of

pytestmark = [pytest.mark.live, pytest.mark.integration]


@pytest.fixture
def notes() -> Notes:
    return Notes()


@pytest.fixture
def registry(notes: Notes) -> Registry:
    key = Settings(_env_file=".env").google_api_key.get_secret_value()  # type: ignore[call-arg]
    if not key:
        pytest.skip("REFLEXO_GOOGLE_API_KEY not set")
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=GeminiProvider(key))
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    reg.provide(OwnerNotifyPort, notes)  # type: ignore[type-abstract]
    return reg


async def say(registry: Registry, db: Database, pnid: str, wa: str, body: str) -> list[str]:
    handle = await turn_for(registry, db, pnid, wa, body)
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    texts = await outbound_texts(db, handle.ref.conversation_id)
    print(f"\n> {body}\n< {texts[-1] if texts else None}")
    return texts


async def placed(registry: Registry, db: Database, bid: UUID, pnid: str, wa: str) -> tuple[Any, UUID]:
    await say(registry, db, pnid, wa, "hi")
    [row] = await _q(db, "SELECT id FROM conversations WHERE thread_key = :t", t=wa)
    conv = row.id
    truffle = next(c for c in await knowledge.search_catalog(bid, "chocolate truffle", k=5) if "Truffle" in c.name)
    kg = next(v for v in truffle.variants if v.name == "1 kg")
    order = await commerce.start_order(
        bid,
        await customer_of(db, conv),
        conv,
        [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=kg.variant_id)],
    )
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""UPDATE orders SET status = 'confirmed', payment_method = 'cod', payment_status = 'cod_pending',
                           delivery_date = current_date + 3, delivery_slot = '4pm-8pm', address = 'Kothrud 411038',
                           details = jsonb_build_object('delivery_date', (current_date + 3)::text,
                                                        'delivery_slot', '4pm-8pm', 'address', 'Kothrud 411038')
                     WHERE id = :o"""),
            {"o": order.order_id},
        )
    return order, conv


async def _q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def status_of(db: Database, order_id: UUID) -> str:
    [row] = await _q(db, "SELECT status FROM orders WHERE id = :o", o=order_id)
    return str(row.status)


async def test_cancel_request_in_hinglish_is_proposed_honestly(registry: Registry, db: Database, notes: Notes) -> None:
    bid, pnid = await make_bakery(db)
    wa = f"9197{new_id().int % 10**8:08d}"
    order, _ = await placed(registry, db, bid, pnid, wa)
    texts = await say(registry, db, pnid, wa, "mujhe apna order cancel karna hai, plans change ho gaye")
    assert await status_of(db, order.order_id) == "confirmed"  # placed + no rule → the owner decides
    [proposal] = await _q(
        db, "SELECT subject, preview FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=bid
    )
    print("\nOWNER BRIEF:", proposal.preview.get("brief"))
    assert proposal.subject["capability"] == "order.cancel"
    assert len(str(proposal.preview.get("brief", ""))) > 20  # the model wrote the owner a real note
    low = texts[-1].lower()
    assert "cancel ho gaya" not in low and "cancelled" not in low  # never claims an outcome it does not have


async def test_with_the_owners_rule_the_agent_cancels_and_says_so(registry: Registry, db: Database) -> None:
    bid, pnid = await make_bakery(db)
    wa = f"9197{new_id().int % 10**8:08d}"
    order, _ = await placed(registry, db, bid, pnid, wa)
    await knowledge.playbook.add_rule(
        bid,
        capability="order.cancel",
        effect="allow",
        conditions=[Condition(field="order.hours_until_delivery", op="gte", value=24.0)],
        says="24 ghante pehle tak cancel free",
        source="owner_instruction",
    )
    texts = await say(registry, db, pnid, wa, "please cancel my order, I won't be home")
    assert await status_of(db, order.order_id) == "cancelled"
    # live 13 Oct: the gate turned the true "cancelled" into "I'm letting the team know" (topic forced to handoff)
    assert "cancel" in texts[-1].lower() and "team" not in texts[-1].lower()


async def test_date_change_request_uses_the_change_tool(registry: Registry, db: Database) -> None:
    bid, pnid = await make_bakery(db)
    wa = f"9197{new_id().int % 10**8:08d}"
    await placed(registry, db, bid, pnid, wa)
    await say(registry, db, pnid, wa, "can you deliver it one day later instead? same time")
    rows = await _q(db, "SELECT subject FROM approval_requests WHERE kind = 'tool_call' AND business_id = :b", b=bid)
    assert rows and rows[0].subject["capability"] == "order.change"
    assert "delivery_date" in rows[0].subject["args"]["changes"]
