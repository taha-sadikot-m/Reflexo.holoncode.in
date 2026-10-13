"""In-call chat (F09, POC_04 §10.5; demo segment 9–11) without LiveKit: routing, the event loop, summary.

A chat message during a live call goes to the voice agent (no chat-agent turn); a photo is described and matched
against the catalogue and lands in the agent's context, or answers a waiting request_chat_media; typed text is
treated as spoken; after the call a recap goes to the chat.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.voice.api as voice_api
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.voice import call_events, sessions
from reflexo.voice.context import start_session
from reflexo.voice.in_call import InCallChat
from reflexo.voice.summary import send_summary
from tests.integration.test_customer_desk import PRIMARY, make_bakery, outbound_texts
from tests.integration.test_gateway import deliver, wa_message

pytestmark = pytest.mark.integration
WA = "919811177788"


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
    voice_api.register(reg)
    return reg


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def caller_on_a_call(registry: Registry, db: Database, status: str = "active") -> dict[str, Any]:
    """A customer who has chatted before (conversation + customer exist) and is now on a call."""
    bid, pnid = await make_bakery(db)
    await deliver(registry, db, "meta_whatsapp", wa_message(pnid, WA, f"wamid.{new_id()}", "hi"), pnid)
    [conv] = await q(db, "SELECT id, customer_id FROM conversations WHERE business_id = :b", b=bid)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("DELETE FROM conversation_turns WHERE conversation_id = :c"), {"c": conv.id})
    session, _ = await sessions.create_connecting(
        business_id=bid,
        provider_call_id=f"wacid.{new_id()}",
        caller_e164=f"+{WA}",
        bridge_host="test",
        customer_id=conv.customer_id,
        member_id=None,
        conversation_id=conv.id,
    )
    if status == "active":
        await sessions.mark_active(session.id)
    return {"bid": bid, "pnid": pnid, "conv": conv.id, "cust": conv.customer_id, "call": session.id}


async def turns(db: Database, conversation_id: UUID) -> int:
    return len(await q(db, "SELECT id FROM conversation_turns WHERE conversation_id = :c", c=conversation_id))


async def test_chat_message_during_a_call_goes_to_the_call_not_the_chat_agent(registry: Registry, db: Database) -> None:
    call = await caller_on_a_call(registry, db)
    await deliver(
        registry, db, "meta_whatsapp", wa_message(call["pnid"], WA, f"wamid.{new_id()}", "Kothrud 411038"), call["pnid"]
    )
    [ev] = await q(db, "SELECT kind, status, payload FROM call_events WHERE call_session_id = :c", c=call["call"])
    assert (str(ev.kind), str(ev.status), ev.payload["text"]) == ("text", "pending", "Kothrud 411038")
    assert await turns(db, call["conv"]) == 0  # the chat agent did not also answer

    claimed = await call_events.claim_pending(call["call"])
    assert [e.text for e in claimed] == ["Kothrud 411038"] and await call_events.claim_pending(call["call"]) == []

    await sessions.mark_ended(call["call"], "caller_hangup")  # after the call, chat is normal again
    await deliver(
        registry, db, "meta_whatsapp", wa_message(call["pnid"], WA, f"wamid.{new_id()}", "thanks"), call["pnid"]
    )
    assert await turns(db, call["conv"]) == 1


async def test_messages_the_call_never_answered_go_back_to_the_chat_ai(registry: Registry, db: Database) -> None:
    """The caller types and hangs up before the agent gets to it: when the call ends, the chat AI answers — one
    turn for the conversation, from the latest unanswered message — and a second sweep finds nothing."""
    call = await caller_on_a_call(registry, db)
    for body in ("Mera order cancel kar do", "please jaldi"):
        await deliver(
            registry, db, "meta_whatsapp", wa_message(call["pnid"], WA, f"wamid.{new_id()}", body), call["pnid"]
        )
    await call_events.claim_pending(call["call"])  # one claimed by a loop that then stopped, both unanswered
    assert await turns(db, call["conv"]) == 0

    await sessions.mark_ended(call["call"], "caller_hangup")
    left = await call_events.unanswered(call["call"])
    assert [e.text for e in left] == ["Mera order cancel kar do", "please jaldi"]
    assert await gateway_api.answer_in_chat(call["bid"], [e.message_id for e in left if e.message_id]) == 1
    [turn] = await q(db, "SELECT message_ids FROM conversation_turns WHERE conversation_id = :c", c=call["conv"])
    texts = await q(db, "SELECT content_text FROM messages WHERE id = ANY(:m)", m=list(turn.message_ids))
    assert "please jaldi" in {t.content_text for t in texts}
    assert await call_events.unanswered(call["call"]) == []  # answered once


async def test_message_while_connecting_is_buffered_for_the_agent(registry: Registry, db: Database) -> None:
    call = await caller_on_a_call(registry, db, status="connecting")
    router = voice_api.CallRouter()
    [msg] = await q(db, "SELECT id FROM messages WHERE conversation_id = :c", c=call["conv"])
    assert await router.deliver_if_active(call["bid"], call["cust"], call["conv"], msg.id, "image") == "buffered"
    [ev] = await call_events.claim_pending(call["call"])
    assert ev.kind == "image"


class FakeSession:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.replies: list[dict[str, Any]] = []

    def say(self, text_: str, **_: Any) -> None:
        self.said.append(text_)

    def generate_reply(self, **kw: Any) -> None:
        self.replies.append(kw)


class FakeChatCtx:
    def __init__(self) -> None:
        self.items: list[str] = []

    def copy(self) -> FakeChatCtx:
        c = FakeChatCtx()
        c.items = list(self.items)
        return c

    def add_message(self, *, role: str, content: str) -> None:
        self.items.append(f"{role}: {content}")


class FakeAgent:
    def __init__(self) -> None:
        self.chat_ctx = FakeChatCtx()

    async def update_chat_ctx(self, ctx: FakeChatCtx) -> None:
        self.chat_ctx = ctx


async def photo_in_chat(registry: Registry, db: Database, call: dict[str, Any]) -> None:
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(
            call["pnid"], WA, f"wamid.{new_id()}", None, type="image", image={"id": "MEDIA1", "mime_type": "image/jpeg"}
        ),
        call["pnid"],
    )
    [msg] = await q(
        db, "SELECT id FROM messages WHERE conversation_id = :c AND content_text = '[image]'", c=call["conv"]
    )
    media = await gateway_api.store_media(
        call["bid"], b"\xff\xd8\xff" + b"0" * 64, "image/jpeg", source="inbound", provider_media_id="MEDIA1"
    )
    async with db.service_session(ServiceReason.ADMIN) as s:  # what media.fetch does once the download finishes
        await s.execute(
            text("UPDATE messages SET attachments = CAST(:a AS jsonb) WHERE id = :m"),
            {"a": json.dumps([{"media_object_id": str(media), "kind": "image", "mime": "image/jpeg"}]), "m": msg.id},
        )


async def test_photo_mid_call_is_described_matched_and_spoken_about(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    call = await caller_on_a_call(registry, db)
    fake.script(
        PRIMARY,
        lambda a: agent.FakeReply(text=json.dumps({"description": "Red velvet cake with cream cheese frosting"})),
    )
    ctx = await start_session(call["call"])
    session, voice_agent = FakeSession(), FakeAgent()
    loop = InCallChat(ctx, session, voice_agent)
    await photo_in_chat(registry, db, call)
    runner = asyncio.create_task(loop.run())
    for _ in range(40):
        if session.replies:
            break
        await asyncio.sleep(0.2)
    loop.stop.set()
    await runner
    assert session.said == ["Photo mil gayi, ek second dekhti hoon."]
    [item] = voice_agent.chat_ctx.items
    assert "Red velvet cake with cream cheese frosting" in item and "Red Velvet Cake (offering_id" in item
    assert 110000 in ctx.allowed_paise  # the voice gate may now say the matched price
    assert session.replies == [{"instructions": "Respond to what the customer just shared, using the matches."}]
    [ev] = await q(db, "SELECT status, payload FROM call_events WHERE call_session_id = :c", c=call["call"])
    assert str(ev.status) == "processed" and "Red Velvet Cake" in ev.payload["matches"]


async def test_requested_photo_resolves_the_waiting_tool_and_text_counts_as_speech(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    call = await caller_on_a_call(registry, db)
    fake.script(PRIMARY, lambda a: agent.FakeReply(text=json.dumps({"description": "Chocolate truffle cake, 2 tiers"})))
    ctx = await start_session(call["call"])
    session, voice_agent = FakeSession(), FakeAgent()
    loop = InCallChat(ctx, session, voice_agent)
    runner = asyncio.create_task(loop.run())
    waiting = loop.next_media()  # request_chat_media is waiting
    await photo_in_chat(registry, db, call)
    result = await asyncio.wait_for(waiting, timeout=10)
    assert "Chocolate truffle cake, 2 tiers" in result and "Chocolate Truffle Cake" in result
    assert session.replies == [] and voice_agent.chat_ctx.items == []  # the tool result carries it

    await deliver(
        registry, db, "meta_whatsapp", wa_message(call["pnid"], WA, f"wamid.{new_id()}", "B-12 Kothrud"), call["pnid"]
    )
    for _ in range(25):
        if session.replies:
            break
        await asyncio.sleep(0.2)
    loop.stop.set()
    await runner
    assert session.replies == [{"user_input": "B-12 Kothrud"}]


async def test_end_of_call_summary_goes_to_the_chat(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    call = await caller_on_a_call(registry, db)
    fake.script(
        PRIMARY, lambda a: agent.FakeReply(text=json.dumps({"summary": "Red velvet 1 kg ka order, 1100 rupees."}))
    )
    ctx = await start_session(call["call"])
    await sessions.append_transcript(call["call"], "caller", "Red velvet 1 kg chahiye")
    await sessions.append_transcript(call["call"], "agent", "Red velvet 1 kg 1100 rupees ka hai.")
    await send_summary(ctx)
    await send_summary(ctx)  # shutdown callbacks can run twice: one message
    texts = await outbound_texts(db, call["conv"])
    assert texts.count("📞 Call summary: Red velvet 1 kg ka order, 1100 rupees.") == 1
