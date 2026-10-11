"""Onboarding end to end on the real (local) Temporal server (F06, POC_05 §3.4; demo segment 1–3).

sources_requested → owner's menu sheet → summary → YES → gap question (only what's missing) → owner answers →
preview of 5 sandbox Q&As → "theek hai" → live. Customers get no AI replies until then.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.worker import Worker

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
import reflexo.workflows.api as workflows_api
from reflexo.common.config import get_settings
from reflexo.common.contracts import TurnRunnerPort, WorkflowPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.workflows import client as temporal_client
from reflexo.workflows.import_activities import ACTIVITIES
from reflexo.workflows.imports import ImportWorkflow
from reflexo.workflows.onboarding import ONBOARDING_ACTIVITIES, OnboardingPreviewWorkflow
from tests.integration.test_ask_owner import bakery, claim_pending, owner_messages, owner_says, q
from tests.integration.test_customer_desk import PRIMARY, turn_for
from tests.integration.test_imports import PRICE_SHEET, XLSX, _xlsx, owner_conversation, owner_file, wait_for

pytestmark = pytest.mark.integration
QUESTIONS = ["Truffle 1kg kitne ka hai?", "Eggless milta hai?", "Delivery Kothrud mein?", "COD hai?", "Timing?"]


@pytest.fixture(autouse=True)
def _own_task_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_TEMPORAL_TASK_QUEUE", f"test-{new_id().hex[-10:]}")
    monkeypatch.setenv("REFLEXO_SPECULATIVE_BUDGET_MS", "3000")
    get_settings.cache_clear()
    temporal_client._client = None
    yield
    temporal_client._client = None
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
    workflows_api.register(reg)
    return reg


@pytest.fixture
async def worker() -> AsyncIterator[None]:
    try:
        c = await temporal_client.client()
    except Exception as exc:  # pragma: no cover - environment
        pytest.skip(f"Temporal not reachable: {exc}")
    async with Worker(
        c,
        task_queue=temporal_client.task_queue(),
        workflows=[ImportWorkflow, OnboardingPreviewWorkflow],
        activities=[*ACTIVITIES, *ONBOARDING_ACTIVITIES],
    ):
        yield


def call(name: str, **args: Any) -> agent.FakeReply:
    return agent.FakeReply(tool_calls=[ToolCall(id=f"c{new_id().hex[-6:]}", name=name, args=args)])


def model(owner_steps: list[agent.FakeReply]) -> Any:
    """One fake for every role: preview question generator, sandbox customer desk, and the owner desk (in order)."""
    remaining = list(owner_steps)

    def script(a: agent.Attempt) -> agent.FakeReply:
        prompt = "\n".join(m.content or "" for m in a.messages)
        if "realistic messages customers would send" in prompt:
            return agent.FakeReply(text=json.dumps({"questions": QUESTIONS}))
        if "chatting with a customer" in prompt:  # sandbox customer desk
            return call(
                "reply",
                action="send",
                blocks=[{"type": "text", "text": "Ji haan, zaroor! 🙂"}],
                topic="greeting_smalltalk",
                language="hi-Latn",
                referenced_records=[],
            )
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return script


async def owner_turn(registry: Registry, db: Database, shop: Any, body: str) -> None:
    await owner_says(registry, db, shop, body)
    [conv] = await q(
        db,
        "SELECT id FROM conversations WHERE channel_connection_id = :tg AND member_id = :m",
        tg=shop.tg,
        m=shop.owner,
    )
    handle = await claim_pending(registry, db, conv.id)
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]


async def eventually(check: Any, within_s: float = 30) -> Any:
    deadline = asyncio.get_running_loop().time() + within_s
    while True:
        result = await check()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met in time ({check.__name__})")
        await asyncio.sleep(0.3)


async def test_onboarding_from_menu_to_live(
    registry: Registry, db: Database, fake: agent.FakeProvider, worker: None
) -> None:
    shop = await bakery(db)
    async with db.service_session(ServiceReason.SEED) as s:  # delivery is the one missing essential
        await s.execute(
            text("""UPDATE business_profiles
                    SET profile = jsonb_set(jsonb_set(profile, '{delivery,zones}', '[]'::jsonb),
                                            '{delivery,pickup_available}', 'false'::jsonb)
                    WHERE business_id = :b"""),
            {"b": shop.bid},
        )
    await owner_api.onboarding.start(shop.bid)
    fake.script(PRIMARY, model([call("reply_owner", text="Ji!")]))

    # not live: a customer message gets no AI turn
    customer = await turn_for_or_none(registry, db, shop)
    assert customer is None

    conv = await owner_conversation(registry, db, shop)
    fid = await owner_file(db, shop, _xlsx(PRICE_SHEET), XLSX, "menu.xlsx", "spreadsheet")
    run_id = await registry.require(WorkflowPort).start_import(  # type: ignore[type-abstract]
        shop.bid, fid, mode="onboarding", member_id=shop.owner, reply_conversation_id=conv
    )
    await wait_for(db, run_id, {"review"})
    assert (await owner_api.onboarding.get(shop.bid))["state"] == "extracting"
    code = re.search(r"YES (\d{4})", (await owner_messages(db, shop))[-1]["text"]).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    await wait_for(db, run_id, {"committed"})

    async def asked_delivery() -> bool:
        return "Delivery kahan tak" in (await owner_messages(db, shop))[-1].get("text", "")

    await eventually(asked_delivery)
    session = await owner_api.onboarding.get(shop.bid)
    assert (session["state"], session["data"]["current"]) == ("gap_questions", "delivery")

    fake.script(
        PRIMARY,
        model(
            [
                call("onboarding_answer", key="delivery", answer="Kothrud aur Baner tak, ₹50 delivery"),
                call("reply_owner", text="Noted! Ek preview bana rahi hoon."),
            ]
        ),
    )
    await owner_turn(registry, db, shop, "Kothrud aur Baner tak, 50 rupees delivery")
    hits = await knowledge.search_knowledge(shop.bid, "delivery charges")
    assert any("Kothrud aur Baner" in h.text for h in hits)  # saved as an owner Q&A for customers

    async def preview_text() -> str | None:  # with a fast fake model it can land before the desk's own reply
        texts = [m.get("text", "") for m in (await owner_messages(db, shop))[-3:]]
        return next((t for t in texts if "Aise jawab dungi" in t), None)

    preview = await eventually(preview_text)
    assert all(question in preview for question in QUESTIONS) and preview.count("💬 Ji haan, zaroor!") == 5
    assert (await owner_api.onboarding.get(shop.bid))["state"] == "preview"

    fake.script(PRIMARY, model([call("onboarding_finish"), call("reply_owner", text="Ho gaya! 🎉")]))
    await owner_turn(registry, db, shop, "theek hai")
    session = await owner_api.onboarding.get(shop.bid)
    [biz] = await q(db, "SELECT status FROM businesses WHERE id = :b", b=shop.bid)
    assert session["state"] == "live" and str(biz.status) == "active"


async def turn_for_or_none(registry: Registry, db: Database, shop: Any) -> Any:
    try:
        return await turn_for(registry, db, shop.pnid, "919899900077", "hi, cake chahiye")
    except Exception:  # no pending turn was scheduled
        return None
