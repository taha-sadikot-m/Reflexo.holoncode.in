"""Owner desk (F04/F07, POC_04 §3.3/§8/§9; demo segment 6–7) through real Telegram/WhatsApp webhooks.

Every write is a read-back + approval: nothing changes until YES <code> / [Confirm] / a bare "haan"; /undo reverts;
one timeline across channels; numbers come from tools.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_ask_owner import Bakery, bakery, claim_pending, owner_messages, owner_says, q
from tests.integration.test_customer_desk import PRIMARY, turn_for
from tests.integration.test_gateway import deliver, wa_message

pytestmark = pytest.mark.integration
OWNER_WA = "+919800000777"


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
    owner_api.register(reg)
    return reg


def call(name: str, **args: Any) -> agent.FakeReply:
    return agent.FakeReply(tool_calls=[ToolCall(id=f"c{new_id().hex[-6:]}", name=name, args=args)])


def say_owner(text_: str) -> agent.FakeReply:
    return call("reply_owner", text=text_)


def tool_results(a: agent.Attempt) -> list[dict[str, Any]]:
    import json

    return [json.loads(m.content or "{}") for m in a.messages if m.role == "tool"]


def steps(
    *replies: Callable[[agent.Attempt], agent.FakeReply] | agent.FakeReply,
) -> Callable[[agent.Attempt], agent.FakeReply]:
    """The n-th model call of the turn gets the n-th reply (n = tool results seen so far + 1)."""

    def script(a: agent.Attempt) -> agent.FakeReply:
        r = replies[min(len(tool_results(a)), len(replies) - 1)]
        return r(a) if callable(r) else r

    return script


async def owner_turn(registry: Registry, db: Database, shop: Bakery, body: str) -> Any:
    """Owner message on Telegram that is not a command → an owner-desk turn, run to completion."""
    await owner_says(registry, db, shop, body)
    [conv] = await q(
        db,
        "SELECT id FROM conversations WHERE channel_connection_id = :tg AND member_id = :m",
        tg=shop.tg,
        m=shop.owner,
    )
    handle = await claim_pending(registry, db, conv.id)
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    return handle


async def truffle_kg_price(db: Database, shop: Bakery) -> int:
    [row] = await q(
        db,
        """SELECT v.price_paise FROM offering_variants v JOIN offerings o ON o.id = v.offering_id
           WHERE o.business_id = :b AND o.name = 'Chocolate Truffle Cake' AND v.name = '1 kg'""",
        b=shop.bid,
    )
    return int(row.price_paise)


def read_back_reply(a: agent.Attempt) -> agent.FakeReply:
    data = tool_results(a)[-1]["data"]
    return say_owner("\n".join(data["read_back"]) + f"\nReply YES {data['approval']['code']} to confirm.")


async def test_price_change_needs_confirmation_and_can_be_undone(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    fake.script(
        PRIMARY,
        steps(
            call(
                "catalog_change",
                ops=[{"action": "update_price", "target": "chocolate truffle 1kg", "fields": {"price": 999}}],
            ),
            read_back_reply,
        ),
    )
    await owner_turn(registry, db, shop, "truffle 1kg ka price 999 kar do")
    msg = (await owner_messages(db, shop))[-1]
    assert "Chocolate Truffle Cake 1 kg price: ₹950 → ₹999" in msg["text"]
    buttons = [b[0]["callback_data"] for b in msg["reply_markup"]["inline_keyboard"]]
    assert re.fullmatch(r"o:apv:[0-9a-f-]{36}:a", buttons[0]) and buttons[1].endswith(":r")
    assert await truffle_kg_price(db, shop) == 95000  # nothing applied before the owner confirms

    code = re.search(r"YES (\d{4})", msg["text"]).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    assert await truffle_kg_price(db, shop) == 99900
    assert "Done ✅" in (await owner_messages(db, shop))[-1]["text"]
    await owner_says(registry, db, shop, f"yes {code}")  # a duplicate YES does nothing twice
    assert "Already done" in (await owner_messages(db, shop))[-1]["text"]

    await owner_says(registry, db, shop, "/undo")
    assert await truffle_kg_price(db, shop) == 95000

    # redo with a bare "haan": the one confirmation this chat is waiting on
    await owner_turn(registry, db, shop, "phir se 999 kar do")
    await owner_says(registry, db, shop, "haan")
    assert await truffle_kg_price(db, shop) == 99900


async def test_ambiguous_size_and_cancel(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    seen: dict[str, Any] = {}

    def after_error(a: agent.Attempt) -> agent.FakeReply:
        seen["error"] = tool_results(a)[-1]["error"]
        return say_owner("Which size — 500 g or 1 kg?")

    fake.script(
        PRIMARY,
        steps(
            call("catalog_change", ops=[{"action": "update_price", "target": "truffle", "fields": {"price": 600}}]),
            after_error,
        ),
    )
    await owner_turn(registry, db, shop, "truffle ka price 600")
    assert seen["error"]["code"] == "AMBIGUOUS_MATCH" and "500 g, 1 kg" in seen["error"]["guidance"]

    fake.script(
        PRIMARY,
        steps(
            call("catalog_change", ops=[{"action": "hide", "target": "pineapple cake", "fields": {}}]), read_back_reply
        ),
    )
    await owner_turn(registry, db, shop, "pineapple cake hata do")
    msg = (await owner_messages(db, shop))[-1]
    cancel = msg["reply_markup"]["inline_keyboard"][1][0]["callback_data"].removeprefix("o:")
    await owner_says(registry, db, shop, callback=cancel)
    [p] = await q(db, "SELECT status FROM offerings WHERE business_id = :b AND name = 'Pineapple Cake'", b=shop.bid)
    assert str(p.status) == "active" and "Cancelled" in (await owner_messages(db, shop))[-1]["text"]


async def test_send_to_customer_and_cod_rule(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    fake.script(
        PRIMARY,
        lambda a: agent.FakeReply(
            tool_calls=[
                ToolCall(
                    id="r",
                    name="reply",
                    args={
                        "action": "send",
                        "blocks": [{"type": "text", "text": "Namaste!"}],
                        "topic": "greeting_smalltalk",
                        "language": "hi-Latn",
                        "referenced_records": [],
                    },
                )
            ]
        ),
    )
    customer_turn = await turn_for(registry, db, shop.pnid, "919811155566", "hi")
    await registry.require(TurnRunnerPort).run_turn(customer_turn)  # type: ignore[type-abstract]
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text(
                "UPDATE customers SET display_name = 'Rahul Verma' "
                "WHERE id = (SELECT customer_id FROM conversations WHERE id = :c)"
            ),
            {"c": customer_turn.ref.conversation_id},
        )

    fake.script(
        PRIMARY,
        steps(
            call("send_to_customer", customer_ref="Rahul", message="Your cake is ready for pickup 🎂"), read_back_reply
        ),
    )
    await owner_turn(registry, db, shop, "Rahul ko bolo cake ready hai")
    assert (
        "📤 To Rahul Verma (whatsapp): “Your cake is ready for pickup 🎂”"
        in (await owner_messages(db, shop))[-1]["text"]
    )
    await owner_says(registry, db, shop, "confirm")
    [sent] = await q(
        db,
        """SELECT m.author, m.content_text FROM messages m WHERE m.conversation_id = :c AND m.direction = 'outbound'
           ORDER BY m.created_at DESC LIMIT 1""",
        c=customer_turn.ref.conversation_id,
    )
    assert (str(sent.author), sent.content_text) == ("owner", "Your cake is ready for pickup 🎂")

    fake.script(
        PRIMARY,
        steps(call("knowledge_change", kind="cod_rule", change={"enabled": True, "max_total": 1000}), read_back_reply),
    )
    await owner_turn(registry, db, shop, "1000 tak COD allow karo")
    assert "Cash on delivery: on, for orders up to ₹1,000" in (await owner_messages(db, shop))[-1]["text"]
    await owner_says(registry, db, shop, "haan")
    assert (await knowledge.business_settings(shop.bid))["cod_rule"] == {"enabled": True, "max_total_paise": 100000}


async def test_one_timeline_across_channels_with_recent_files(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO owner_channel_bindings (id, business_id, member_id, channel, external_id, verified_at)
                    VALUES (:id, :b, :m, 'whatsapp', :e, now())"""),
            {"id": new_id(), "b": shop.bid, "m": shop.owner, "e": OWNER_WA},
        )
        await s.execute(  # the shop number serves customers and the owner (as seeded)
            text("UPDATE channel_connections SET roles = '{customer,owner}' WHERE business_id = :b"), {"b": shop.bid}
        )
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(shop.pnid, OWNER_WA[1:], f"wamid.{new_id()}", "naya price sheet bhej rahi hoon"),
        shop.pnid,
    )
    media = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO media_objects (id, business_id, storage_key, mime, size_bytes, sha256, source)
                    VALUES (:id, :b, :k, 'application/vnd.ms-excel', 10, :h, 'inbound')"""),
            {"id": media, "b": shop.bid, "k": f"t/{media}", "h": media.hex},
        )
        await s.execute(
            text("""INSERT INTO owner_files (id, business_id, member_id, media_object_id, filename, kind,
                                            description, received_via)
                    VALUES (:id, :b, :m, :mo, 'prices_oct.xlsx', 'spreadsheet', 'Excel: 3 rows — Item, Price',
                            'whatsapp')"""),
            {"id": new_id(), "b": shop.bid, "m": shop.owner, "mo": media},
        )
    seen: dict[str, str] = {}

    def look(a: agent.Attempt) -> agent.FakeReply:
        seen["prompt"] = "\n".join(m.content or "" for m in a.messages)
        return say_owner("Got it.")

    fake.script(PRIMARY, look)
    await owner_turn(registry, db, shop, "jo sheet bheji usse prices update karo")
    assert "[WhatsApp]" in seen["prompt"] and "naya price sheet bhej rahi hoon" in seen["prompt"]
    assert re.search(
        r"\[file:[0-9a-f-]{36}\] spreadsheet \"prices_oct.xlsx\" — Excel: 3 rows — Item, Price — whatsapp",
        seen["prompt"],
    )
    assert "[Telegram]" in seen["prompt"]


async def test_stats_come_from_the_database(registry: Registry, db: Database) -> None:
    shop = await bakery(db)
    ctx = agent.ToolContext(
        business_id=shop.bid,
        desk="owner",
        channel="telegram",
        conversation_id=None,
        customer_id=None,
        member_id=shop.owner,
        turn_id=new_id(),
    )
    res = await agent.run_tool(
        ToolCall(id="1", name="business_stats", args={"period": "today"}), ctx, {"business_stats"}
    )
    assert res["data"]["orders_confirmed"] == 0 and res["data"]["revenue_paid"] == "₹0"
    denied = await agent.run_tool(ToolCall(id="2", name="catalog_change", args={"ops": []}), ctx, {"business_stats"})
    assert denied["error"]["code"] == "FORBIDDEN_SCOPE"  # only bound tools run


async def test_owner_turn_failure_tells_the_owner(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    shop = await bakery(db)
    down = agent.FakeReply(error=agent.ModelError("PROVIDER_UNAVAILABLE", "503"))
    fake.script(PRIMARY, lambda a: down)
    from tests.integration.test_customer_desk import FALLBACK

    fake.script(FALLBACK, lambda a: down)
    await owner_turn(registry, db, shop, "aaj kitne orders aaye?")
    assert "couldn't finish that" in (await owner_messages(db, shop))[-1]["text"]


async def test_media_describe_fills_owner_file_and_customer_photo(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    import json

    from reflexo.agent import media_describe

    shop = await bakery(db)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    owner_media = await gateway_api.store_media(shop.bid, png, "image/png", source="inbound", provider_media_id=None)
    customer_media = await gateway_api.store_media(
        shop.bid, png + b"1", "image/png", source="inbound", provider_media_id=None
    )
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO owner_files (id, business_id, member_id, media_object_id, kind, received_via)
                    VALUES (:id, :b, :m, :mo, 'image', 'whatsapp')"""),
            {"id": new_id(), "b": shop.bid, "m": shop.owner, "mo": owner_media},
        )
    prompts: list[str] = []

    def vision(a: agent.Attempt) -> agent.FakeReply:
        prompt = a.messages[-1].content or ""
        prompts.append(prompt)
        assert a.messages[-1].media and a.messages[-1].media[0].mime == "image/png"
        text_ = "Menu photo, 6 cakes with prices" if "file library" in prompt else "Two-tier red velvet cake"
        return agent.FakeReply(text=json.dumps({"description": text_}))

    fake.script(PRIMARY, vision)
    gw = agent.gateway()
    assert (
        await media_describe.describe(gw, shop.bid, owner_media, owner_file=True) == "Menu photo, 6 cakes with prices"
    )
    assert await media_describe.describe(gw, shop.bid, customer_media, owner_file=False) == "Two-tier red velvet cake"
    [f] = await q(db, "SELECT description FROM owner_files WHERE media_object_id = :m", m=owner_media)
    [m] = await q(db, "SELECT description FROM media_objects WHERE id = :m", m=customer_media)
    assert (f.description, m.description) == ("Menu photo, 6 cakes with prices", "Two-tier red velvet cake")


def test_read_back_lines_are_always_shown() -> None:
    from reflexo.agent.owner_desk import reply_blocks

    found = [{"id": "x" * 36, "code": "1234", "read_back": "✏️ Truffle 1 kg price: ₹950 → ₹999"}]
    blocks = reply_blocks("Reply YES 1234 to confirm.", found)  # the model forgot the line
    assert blocks[0]["text"].startswith("✏️ Truffle 1 kg price: ₹950 → ₹999\n\nReply YES 1234")
    kept = reply_blocks("✏️ Truffle 1 kg price: ₹950 → ₹999\nReply YES 1234", found)
    assert kept[0]["text"].count("₹950 → ₹999") == 1


async def test_adding_an_existing_product_updates_its_price(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    """Live: "Custom Photo Cake: price 1500" created a second Custom Photo Cake instead of pricing the first."""
    shop = await bakery(db)
    fake.script(
        PRIMARY,
        steps(
            call("catalog_change", ops=[{"action": "add", "target": "Custom Photo Cake", "fields": {"price": 1500}}]),
            read_back_reply,
        ),
    )
    await owner_turn(registry, db, shop, "Custom Photo Cake :- price 1500")
    assert "✏️ Custom Photo Cake base price: — → ₹1,500" in (await owner_messages(db, shop))[-1]["text"]  # edit, not ➕
    await owner_says(registry, db, shop, "haan")
    rows = await q(
        db, "SELECT base_price_paise FROM offerings WHERE business_id = :b AND name = 'Custom Photo Cake'", b=shop.bid
    )
    assert [r.base_price_paise for r in rows] == [150000]


async def test_typed_sentence_never_decides_a_pending_confirmation(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    """Live: a rules sentence containing "confirm" answered "Already done ✅" instead of reaching the desk."""
    shop = await bakery(db)
    fake.script(
        PRIMARY,
        steps(call("knowledge_change", kind="cod_rule", change={"enabled": True, "max_total": 1500}), read_back_reply),
    )
    await owner_turn(registry, db, shop, "1500 tak COD allow karo")
    await owner_says(registry, db, shop, "haan")
    fake.script(PRIMARY, steps(say_owner("Rules bana rahi hoon.")))
    await owner_turn(registry, db, shop, "Do not take more than 8 orders a day, confirm COD orders and send reminders")
    assert (await owner_messages(db, shop))[-1]["text"] == "Rules bana rahi hoon."  # reached the owner desk
