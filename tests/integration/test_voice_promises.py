"""One employee on every channel, and no promise without a record (live 14 Oct).

On a call the customer sent a design photo for a custom cake; the agent said "I'll confirm the price with the
owner and message you on WhatsApp" — but the voice agent had no way to ask the owner: nothing reached the owner,
the owner never saw the photo, and the recap claimed an order that did not exist.

Now: the voice agent asks the owner with the same loop as chat (the owner gets the photo; the answer is relayed
to the WhatsApp chat), the recap is written from what the system recorded, and any promise no record covers
becomes a question to the owner at the end of the call.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
import reflexo.voice.api as voice_api
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.common.timeutil import utcnow
from reflexo.pipeline import api as pipeline_api
from reflexo.voice import sessions
from reflexo.voice.context import start_session
from reflexo.voice.summary import send_summary
from tests.integration.test_ask_owner import Bakery, bakery, owner_messages, owner_says, q, wa_number
from tests.integration.test_customer_desk import PRIMARY, outbound_texts
from tests.integration.test_gateway import deliver, wa_message

pytestmark = pytest.mark.integration


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
    owner_api.register(reg)  # the real notifier (Telegram rows) and owner commands
    voice_api.register(reg)
    return reg


async def on_a_call_with_a_photo(registry: Registry, db: Database, shop: Bakery) -> dict[str, Any]:
    """A customer who chatted before, is now on a call, and has sent a design photo in the chat."""
    wa = wa_number()
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, wa, f"wamid.{new_id()}", "hi"), shop.pnid)
    [conv] = await q(db, "SELECT id, customer_id FROM conversations WHERE thread_key = :t", t=wa)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("DELETE FROM conversation_turns WHERE conversation_id = :c"), {"c": conv.id})
    media = await gateway_api.store_media(
        shop.bid, b"\xff\xd8\xff" + b"0" * 64, "image/jpeg", source="inbound", provider_media_id=f"M{new_id().hex}"
    )
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                          channel_connection_id, content_text, attachments, status)
                    SELECT :id, c.business_id, c.id, 'inbound', 'customer', c.channel, c.channel_connection_id,
                           '[image]', CAST(:a AS jsonb), 'received' FROM conversations c WHERE c.id = :c"""),
            {
                "id": new_id(),
                "c": conv.id,
                "a": json.dumps([{"media_object_id": str(media), "kind": "image", "mime": "image/jpeg"}]),
            },
        )
    session, _ = await sessions.create_connecting(
        business_id=shop.bid,
        provider_call_id=f"wacid.{new_id()}",
        caller_e164=f"+{wa}",
        bridge_host="test",
        customer_id=conv.customer_id,
        member_id=None,
        conversation_id=conv.id,
    )
    await sessions.mark_active(session.id)
    return {"conv": conv.id, "cust": conv.customer_id, "call": session.id, "media": media}


async def test_the_voice_agent_asks_the_owner_who_sees_the_photo_and_answers_into_the_chat(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    call = await on_a_call_with_a_photo(registry, db, shop)
    ctx = agent.ToolContext(
        business_id=shop.bid,
        desk="voice",
        channel="voice",
        conversation_id=call["conv"],
        customer_id=call["cust"],
        member_id=None,
        turn_id=None,
        call_session_id=call["call"],
        step=1,
    )
    result = await agent.run_tool(
        ToolCall(
            id="t1",
            name="ask_owner",
            args={
                "question": "Price for this design (photo in chat) as a 1 kg chocolate cake, pickup Fri 16 Oct?",
                "note_for_owner": "Taha called: wants the cake in the photo, 1 kg, pickup Friday. No message.",
            },
        ),
        ctx,
        set(agent.VOICE_TOOLS),
    )
    assert result["ok"] and result["data"]["outcome"] == "asked_owner", result
    code = result["data"]["question_code"]
    [question] = await q(
        db,
        "SELECT question, status FROM owner_questions WHERE short_code = :c AND business_id = :b",
        c=code,
        b=shop.bid,
    )
    assert "1 kg chocolate cake" in question.question

    told = [json.dumps(m, ensure_ascii=False) for m in await owner_messages(db, shop)]
    photo = next(i for i, m in enumerate(told) if "photo" in m.lower() and str(call["media"]) in m)
    ask = next(i for i, m in enumerate(told) if f"Question {code}" in m)
    assert photo < ask  # the owner sees the design first, then the question
    assert "phone call" in told[ask] and "Taha called" in told[ask]

    await owner_says(registry, db, shop, f"{code} 1500 rupees for 1 kg, ready Friday")
    relay = await q(
        db, "SELECT id FROM conversation_turns WHERE conversation_id = :c AND status = 'pending'", c=call["conv"]
    )
    assert relay  # the answer goes to the customer's WhatsApp chat through the usual relay turn


async def test_a_promise_with_no_record_becomes_a_question_and_the_recap_states_only_facts(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    call = await on_a_call_with_a_photo(registry, db, shop)
    seen: list[str] = []

    def review(a: agent.Attempt) -> agent.FakeReply:
        seen.append("\n".join(m.content or "" for m in a.messages))
        return agent.FakeReply(
            text=json.dumps(
                {
                    "summary": "Aapne photo wala design 1 kg mein manga, pickup Fri 16 Oct. Price shop WhatsApp par "
                    "batayegi.",
                    "open_promises": ["Price of the chocolate drip cake in the photo, 1 kg, pickup Fri 16 Oct"],
                }
            )
        )

    fake.script(PRIMARY, review)
    ctx = await start_session(call["call"])
    await sessions.append_transcript(call["call"], "caller", "Yeh design 1 kg chahiye, Friday pickup")
    await sessions.append_transcript(call["call"], "agent", "Main owner se price confirm karke WhatsApp par bataungi.")
    await send_summary(ctx)
    await send_summary(ctx)  # shutdown callbacks can fire twice: one recap, one follow-up

    assert "nothing (no order was placed" in seen[0]  # the recap is written against the recorded facts
    asked = await q(db, "SELECT question FROM owner_questions WHERE conversation_id = :c", c=call["conv"])
    assert [a.question for a in asked] == ["Price of the chocolate drip cake in the photo, 1 kg, pickup Fri 16 Oct"]
    told = json.dumps(await owner_messages(db, shop), ensure_ascii=False)
    assert "they are waiting for your answer" in told and str(call["media"]) in told
    recaps = [t for t in await outbound_texts(db, call["conv"]) if t.startswith("📞 Call summary")]
    assert len(recaps) == 1
    [stored] = await q(db, "SELECT summary FROM call_sessions WHERE id = :c", c=call["call"])
    assert stored.summary.startswith("Aapne photo wala design")


async def test_the_owner_gets_the_photos_of_this_request_not_an_earlier_one(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    """Live 14 Oct: the design from an earlier call (24 min before) was sent along with the new one."""
    shop = await bakery(db)
    call = await on_a_call_with_a_photo(registry, db, shop)  # the photo of THIS request, just now
    older, extra = new_id(), new_id()
    async with db.service_session(ServiceReason.ADMIN) as s:
        # an earlier, separate request (25 min ago) and a photo sent together with this one (3 min ago)
        for mid, age in ((older, timedelta(minutes=25)), (extra, timedelta(minutes=3))):
            await s.execute(
                text("""INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                              channel_connection_id, content_text, attachments, status, created_at)
                        SELECT :id, c.business_id, c.id, 'inbound', 'customer', c.channel, c.channel_connection_id,
                               '[image]', CAST(:a AS jsonb), 'received', :at
                        FROM conversations c WHERE c.id = :c"""),
                {
                    "id": new_id(),
                    "c": call["conv"],
                    "at": utcnow() - age,
                    "a": json.dumps([{"media_object_id": str(mid), "kind": "image", "mime": "image/jpeg"}]),
                },
            )
    async with db.tenant_session(shop.bid) as s:
        media = await gateway_api.recent_customer_media(s, call["conv"])
    assert media == [call["media"], extra]  # this request's photos, newest first; not the earlier one
