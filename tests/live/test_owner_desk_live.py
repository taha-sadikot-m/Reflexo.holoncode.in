"""Owner desk with REAL Gemini on a real database.

Opt-in: `uv run pytest --live tests/live/test_owner_desk_live.py -s`.

WP-10 done-when (demo segments 5–7): Hinglish owner commands produce exact read-backs that apply only after the
owner confirms; ambiguity is asked about; numbers come from tools; free-text answers reach the waiting customer.
Nothing is sent to real Telegram/WhatsApp (outbound rows only).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
from reflexo.agent.model_gateway.providers import GeminiProvider
from reflexo.common.config import Settings
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_ask_owner import Bakery, bakery, claim_pending, owner_messages, owner_says, q
from tests.integration.test_customer_desk import turn_for

pytestmark = [pytest.mark.live, pytest.mark.integration]


@pytest.fixture
def registry() -> Registry:
    key = Settings(_env_file=".env").google_api_key.get_secret_value()  # type: ignore[call-arg]
    if not key:
        pytest.skip("REFLEXO_GOOGLE_API_KEY not set")
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=GeminiProvider(key))
    knowledge.register(reg)
    owner_api.register(reg)
    return reg


async def owner_turn(registry: Registry, db: Database, shop: Bakery, body: str) -> tuple[str, list[str], float]:
    """Owner message on Telegram → owner-desk turn with real Gemini. Returns (reply text, tools used, seconds)."""
    await owner_says(registry, db, shop, body)
    [conv] = await q(
        db,
        "SELECT id FROM conversations WHERE channel_connection_id = :tg AND member_id = :m",
        tg=shop.tg,
        m=shop.owner,
    )
    handle = await claim_pending(registry, db, conv.id)
    started = time.perf_counter()
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    elapsed = time.perf_counter() - started
    tools = [
        str(r.attrs.get("tool"))
        for r in await q(
            db,
            "SELECT attrs FROM pipeline_events WHERE turn_id = :t AND stage = 'tool_call' ORDER BY created_at",
            t=handle.ref.turn_id,
        )
    ]
    reply = (await owner_messages(db, shop))[-1]
    print(f"\n{elapsed:.1f}s tools={tools} :: {body!r}\n  → {reply.get('text')}\n  buttons={_buttons(reply)}")
    return str(reply.get("text")), tools, elapsed


def _buttons(msg: dict[str, Any]) -> list[str]:
    return [row[0]["text"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])]


async def truffle_kg(db: Database, shop: Bakery) -> int:
    [row] = await q(
        db,
        """SELECT v.price_paise FROM offering_variants v JOIN offerings o ON o.id = v.offering_id
           WHERE o.business_id = :b AND o.name = 'Chocolate Truffle Cake' AND v.name = '1 kg'""",
        b=shop.bid,
    )
    return int(row.price_paise)


async def test_price_change_confirm_undo_live(registry: Registry, db: Database) -> None:
    shop = await bakery(db)
    reply, tools, _ = await owner_turn(registry, db, shop, "Chocolate truffle 1kg ka price 999 kar do")
    assert "catalog_change" in tools and "₹950 → ₹999" in reply
    assert _buttons((await owner_messages(db, shop))[-1]) == ["✅ Confirm", "✖ Cancel"]
    assert await truffle_kg(db, shop) == 95000
    await owner_says(registry, db, shop, "haan")
    assert await truffle_kg(db, shop) == 99900
    await owner_says(registry, db, shop, "/undo")
    assert await truffle_kg(db, shop) == 95000


async def test_ambiguous_size_is_asked_live(registry: Registry, db: Database) -> None:
    shop = await bakery(db)
    reply, _, _ = await owner_turn(registry, db, shop, "truffle ka price 600 karo")
    assert "500" in reply and "1 kg" in reply.replace("1kg", "1 kg")
    assert await q(db, "SELECT id FROM approval_requests WHERE business_id = :b", b=shop.bid) == []


async def test_stats_and_add_item_live(registry: Registry, db: Database) -> None:
    shop = await bakery(db)
    reply, tools, _ = await owner_turn(registry, db, shop, "aaj kitne orders aaye?")
    assert "business_stats" in tools or "orders_list" in tools
    assert any(w in reply.lower() for w in ("0", "koi", "no ", "none", "nahi"))  # "Aaj koi order nahi aaya"
    reply, tools, _ = await owner_turn(registry, db, shop, "naya item add karo: Lemon Tart ₹380")
    assert "catalog_change" in tools and "Lemon Tart" in reply and "₹380" in reply
    await owner_says(registry, db, shop, "yes")
    assert await knowledge.resolve_offerings(shop.bid, "lemon tart")


async def test_free_text_answer_reaches_waiting_customer_live(registry: Registry, db: Database) -> None:
    shop = await bakery(db)
    customer = await turn_for(registry, db, shop.pnid, "919899900011", "Kya aap sugar-free cake banate ho?")
    await registry.require(TurnRunnerPort).run_turn(customer)  # type: ignore[type-abstract]
    [question] = await q(
        db, "SELECT id, status FROM owner_questions WHERE conversation_id = :c", c=customer.ref.conversation_id
    )
    reply, tools, _ = await owner_turn(registry, db, shop, "haan sugar-free bhi banate hain, order pe, 100 extra")
    assert "answer_pending" in tools
    [after] = await q(db, "SELECT status, answer FROM owner_questions WHERE id = :i", i=question.id)
    assert str(after.status) in ("answered", "relayed") and "100" in after.answer
    print("customer question answered:", after.answer, "| owner reply:", reply)
