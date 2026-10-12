"""Ask-the-owner loop end to end (F05, POC_04 §7; demo segment 5–6) through real webhooks.

Customer asks on WhatsApp → holding reply + question on the owner's Telegram (buttons) → the owner answers on
Telegram (`1234 …`, a button, or a swipe-reply) → scope → Q&A saved → relay turn answers the customer → the next
customer gets it from knowledge. Plus the SLA reminder and `/undo` `/pause` `/resume` `/help`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
from reflexo.common.contracts import QuestionAnswerPort, TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.pipeline.turns import _TURN_COLUMNS, PipelineTurnHandle, TurnService, _ref
from tests.integration.test_customer_desk import PRIMARY, make_bakery, outbound_texts, reply, turn_for
from tests.integration.test_gateway import deliver

pytestmark = pytest.mark.integration
OWNER_CHAT = "555001"


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
def registry(fake: agent.FakeProvider) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=fake)
    knowledge.register(reg)
    owner_api.register(reg)  # real notifier (Telegram outbound rows) + real deterministic commands
    return reg


@dataclass
class Bakery:
    bid: UUID
    pnid: str
    tg: UUID
    owner: UUID


async def bakery(db: Database) -> Bakery:
    bid, pnid = await make_bakery(db)
    tg, owner = new_id(), new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                                     display_name, credential_ref, roles)
                    VALUES (:id, :b, 'telegram', 'telegram', :e, 'TG', 'REFLEXO_TELEGRAM', '{customer,owner}')"""),
            {"id": tg, "b": bid, "e": f"tg-{tg.hex[-8:]}"},
        )
        await s.execute(
            text("INSERT INTO team_members (id, business_id, display_name, role) VALUES (:id, :b, 'Meera', 'owner')"),
            {"id": owner, "b": bid},
        )
        await s.execute(
            text("""INSERT INTO owner_channel_bindings (id, business_id, member_id, channel, external_id, verified_at)
                    VALUES (:id, :b, :m, 'telegram', :e, now())"""),
            {"id": new_id(), "b": bid, "m": owner, "e": OWNER_CHAT},
        )
    return Bakery(bid, pnid, tg, owner)


def classifier_or(desk_reply: Any, scope: str = "global", confidence: float = 0.92) -> Any:
    """One fake model for both roles: the scope classifier prompt gets JSON, everything else the desk reply."""

    def script(a: agent.Attempt) -> agent.FakeReply:
        if any("A shop owner answered" in (m.content or "") for m in a.messages):
            return agent.FakeReply(text=json.dumps({"scope": scope, "confidence": confidence}))
        return desk_reply(a) if callable(desk_reply) else desk_reply

    return script


def ask_eggless() -> agent.FakeReply:
    return reply(
        "ask_owner",
        None,
        topic="catalog_info",
        owner_question={"question": "Eggless option hai?", "ai_guess": "not sure", "options": ["Haan", "Nahi"]},
    )


async def owner_says(
    registry: Registry,
    db: Database,
    shop: Bakery,
    body: str | None = None,
    *,
    reply_to: int | None = None,
    callback: str | None = None,
) -> None:
    n = new_id().int % 10**9
    if callback is not None:
        update: dict[str, Any] = {
            "update_id": n,
            "callback_query": {
                "id": str(n),
                "from": {"id": int(OWNER_CHAT), "first_name": "Meera"},
                "message": {"message_id": 1, "chat": {"id": int(OWNER_CHAT)}},
                "data": f"o:{callback}",
            },
        }
    else:
        msg: dict[str, Any] = {
            "message_id": n,
            "date": 1760000000,
            "chat": {"id": int(OWNER_CHAT)},
            "from": {"id": int(OWNER_CHAT), "first_name": "Meera"},
            "text": body,
        }
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": reply_to}
        update = {"update_id": n, "message": msg}
    await deliver(registry, db, "telegram", update, str(shop.tg), path=str(shop.tg))


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def owner_messages(db: Database, shop: Bakery) -> list[dict[str, Any]]:
    rows = await q(
        db,
        """SELECT o.rendered_payload FROM outbound_messages o JOIN conversations c ON c.id = o.conversation_id
           WHERE c.channel_connection_id = :tg ORDER BY o.created_at, o.part_index""",
        tg=shop.tg,
    )
    return [dict(r.rendered_payload) for r in rows]


async def claim_pending(registry: Registry, db: Database, conversation_id: UUID) -> PipelineTurnHandle:
    async with db.service_session(ServiceReason.ADMIN) as s:
        row = (
            await s.execute(
                text(f"""UPDATE conversation_turns SET status = 'running', lease_owner = 'test', started_at = now(),
                             lease_until = now() + interval '120 seconds', attempts = attempts + 1
                         WHERE id = (SELECT id FROM conversation_turns WHERE conversation_id = :c
                                     AND status = 'pending' ORDER BY created_at DESC LIMIT 1)
                         RETURNING {_TURN_COLUMNS}"""),
                {"c": conversation_id},
            )
        ).one()
    return PipelineTurnHandle(_ref(row), "test", registry.require(TurnService))


async def asked(registry: Registry, db: Database, fake: agent.FakeProvider, shop: Bakery, wa: str) -> tuple[Any, Any]:
    fake.script(PRIMARY, classifier_or(ask_eggless()))
    handle = await turn_for(registry, db, shop.pnid, wa, "Eggless option hai?")
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    [question] = await q(db, "SELECT * FROM owner_questions WHERE conversation_id = :c", c=handle.ref.conversation_id)
    return handle, question


def wa_number() -> str:
    return f"9197{new_id().int % 10**8:08d}"


# ---------------------------------------------------------------- the loop (demo segment 5–6)


async def test_owner_answer_on_telegram_is_relayed_saved_and_reused(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    handle, question = await asked(registry, db, fake, shop, wa_number())
    code = question.short_code.strip()
    conv = handle.ref.conversation_id

    [note] = await owner_messages(db, shop)
    assert f"Question {code}" in note["text"] and "Eggless option hai?" in note["text"]
    keyboard = [b[0]["callback_data"] for b in note["reply_markup"]["inline_keyboard"]]
    assert keyboard == [
        f"o:oq:{question.id}:0",
        f"o:oq:{question.id}:1",
        f"o:oq:{question.id}:later",
        f"o:chat:{conv}",  # the conversation is one tap away, never pasted in
    ]
    [sla] = await q(
        db,
        "SELECT run_after > now() + interval '10 minutes' AS later FROM jobs WHERE dedupe_key = :k",
        k=f"oqsla:{question.id}",
    )
    assert sla.later

    await owner_says(registry, db, shop, f"{code} Haan, sab cakes eggless bhi milte hain, ₹50 extra")
    [answered] = await q(
        db, "SELECT status, answer_scope, qa_entry_id FROM owner_questions WHERE id = :i", i=question.id
    )
    assert (str(answered.status), str(answered.answer_scope)) == ("answered", "global")
    [qa] = await q(db, "SELECT scope, customer_id, source FROM qa_entries WHERE id = :i", i=answered.qa_entry_id)
    assert (str(qa.scope), qa.customer_id, qa.source) == ("global", None, "owner_chat")
    assert "Sending your answer" in (await owner_messages(db, shop))[-1]["text"]

    relay = await claim_pending(registry, db, conv)
    assert relay.ref.hints == {"relay_question_id": str(question.id)}

    def relay_reply(a: agent.Attempt) -> agent.FakeReply:
        prompt = "\n".join(m.content or "" for m in a.messages)
        qa_id = re.search(r"THE OWNER ANSWERED.*?saved as qa:([0-9a-f-]{36})", prompt, re.DOTALL)
        assert qa_id is not None
        return reply(
            "send",
            "Haan ji, sab cakes eggless bhi milte hain, ₹50 extra.",
            topic="catalog_info",
            refs=[{"type": "qa", "id": qa_id.group(1)}],
        )

    fake.script(PRIMARY, classifier_or(relay_reply))
    await registry.require(TurnRunnerPort).run_turn(relay)  # type: ignore[type-abstract]
    assert (await outbound_texts(db, conv))[-1] == "Haan ji, sab cakes eggless bhi milte hain, ₹50 extra."
    [done] = await q(db, "SELECT status, relayed_at FROM owner_questions WHERE id = :i", i=question.id)
    assert str(done.status) == "relayed" and done.relayed_at is not None

    other = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(text("INSERT INTO customers (id, business_id) VALUES (:id, :b)"), {"id": other, "b": shop.bid})
    hits = await knowledge.search_knowledge(shop.bid, "eggless option hai", customer_id=other)
    assert any(h.entity_id == answered.qa_entry_id for h in hits)  # the next customer: answered from knowledge

    await owner_says(registry, db, shop, f"{code} haan")  # a repeated answer does nothing twice
    assert "already answered" in (await owner_messages(db, shop))[-1]["text"]


async def test_uncertain_scope_is_kept_private_until_the_owner_taps(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    _, question = await asked(registry, db, fake, shop, wa_number())
    fake.script(PRIMARY, classifier_or(ask_eggless(), scope="global", confidence=0.4))
    await owner_says(registry, db, shop, f"{question.short_code.strip()} haan 50 extra")
    [row] = await q(db, "SELECT qa_entry_id, answer_scope FROM owner_questions WHERE id = :i", i=question.id)
    [qa] = await q(db, "SELECT scope, customer_id FROM qa_entries WHERE id = :i", i=row.qa_entry_id)
    assert (str(qa.scope), qa.customer_id) == ("customer", question.customer_id)  # never leaks while unsure
    scope_msg = (await owner_messages(db, shop))[-1]
    assert "should it apply to everyone?" in scope_msg["text"]
    assert scope_msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"o:oqs:{question.id}:g"

    await owner_says(registry, db, shop, callback=f"oqs:{question.id}:g")
    [qa] = await q(db, "SELECT scope, customer_id FROM qa_entries WHERE id = :i", i=row.qa_entry_id)
    assert (str(qa.scope), qa.customer_id) == ("global", None)
    assert "Saved for everyone" in (await owner_messages(db, shop))[-1]["text"]


async def test_button_tap_and_swipe_reply_answer_the_question(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    _, first = await asked(registry, db, fake, shop, wa_number())
    await owner_says(registry, db, shop, callback=f"oq:{first.id}:0")
    [row] = await q(db, "SELECT status, answer FROM owner_questions WHERE id = :i", i=first.id)
    assert (str(row.status), row.answer) == ("answered", "Haan")

    _, second = await asked(registry, db, fake, shop, wa_number())
    async with db.service_session(ServiceReason.ADMIN) as s:  # the provider id Telegram gave the question message
        await s.execute(
            text("""UPDATE outbound_messages SET provider_message_id = '9001'
                    WHERE idempotency_key LIKE 'notify:%' AND rendered_payload->>'text' LIKE :t"""),
            {"t": f"%Question {second.short_code.strip()}%"},
        )
    await owner_says(registry, db, shop, "sirf isko 10% off de do", reply_to=9001)
    [row] = await q(db, "SELECT status, answer, answer_scope FROM owner_questions WHERE id = :i", i=second.id)
    assert (str(row.status), row.answer, str(row.answer_scope)) == ("answered", "sirf isko 10% off de do", "customer")


async def test_i_will_reply_button_and_sla_reminder(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    handle, question = await asked(registry, db, fake, shop, wa_number())
    await owner_says(registry, db, shop, callback=f"oq:{question.id}:later")
    assert f"Reply `{question.short_code.strip()} &lt;answer&gt;`" in (await owner_messages(db, shop))[-1]["text"]

    service = registry.require(QuestionAnswerPort)  # type: ignore[type-abstract]
    for _ in range(2):  # job retried: one reminder only
        await service.run_sla(shop.bid, question.id)  # type: ignore[attr-defined]
    texts = await outbound_texts(db, handle.ref.conversation_id)
    assert texts.count("Hamari team jaldi aapko batayegi 🙏") == 1
    await owner_says(registry, db, shop, f"{question.short_code.strip()} Nahi")
    await service.run_sla(shop.bid, question.id)  # type: ignore[attr-defined]
    assert (await outbound_texts(db, handle.ref.conversation_id)).count("Hamari team jaldi aapko batayegi 🙏") == 1


async def test_undo_pause_resume_help(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    _, question = await asked(registry, db, fake, shop, wa_number())
    await owner_says(registry, db, shop, f"{question.short_code.strip()} Haan, ₹50 extra")
    [row] = await q(db, "SELECT qa_entry_id FROM owner_questions WHERE id = :i", i=question.id)

    await owner_says(registry, db, shop, "/undo")
    [qa] = await q(db, "SELECT status FROM qa_entries WHERE id = :i", i=row.qa_entry_id)
    assert str(qa.status) == "archived" and "↩️ Undo:" in (await owner_messages(db, shop))[-1]["text"]

    await owner_says(registry, db, shop, "/pause")
    modes = await q(
        db,
        "SELECT customer_mode FROM channel_connections WHERE business_id = :b AND 'customer' = ANY(roles)",
        b=shop.bid,
    )
    assert {str(m.customer_mode) for m in modes} == {"off"}
    await owner_says(registry, db, shop, "/resume")
    modes = await q(
        db,
        "SELECT customer_mode FROM channel_connections WHERE business_id = :b AND 'customer' = ANY(roles)",
        b=shop.bid,
    )
    assert {str(m.customer_mode) for m in modes} == {"tiered"}
    await owner_says(registry, db, shop, "/help")
    assert "/undo" in (await owner_messages(db, shop))[-1]["text"]
    # none of these became owner-desk turns
    turns = await q(
        db,
        """SELECT t.id FROM conversation_turns t JOIN conversations c ON c.id = t.conversation_id
                           WHERE c.channel_connection_id = :tg""",
        tg=shop.tg,
    )
    assert turns == []


async def test_show_chat_button_brings_the_conversation_on_demand(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    handle, _ = await asked(registry, db, fake, shop, wa_number())
    await owner_says(registry, db, shop, callback=f"chat:{handle.ref.conversation_id}")
    last = (await owner_messages(db, shop))[-1]["text"]
    assert last.startswith("💬 Last messages") and "Eggless option hai?" in last
