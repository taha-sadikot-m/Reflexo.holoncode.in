"""AutomationBuildWorkflow on the real (local) Temporal server (CHK-F08-1; POC_06 §4; demo segment 7–9)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from temporalio.worker import Worker

import reflexo.agent.api as agent
import reflexo.automations.api as automations
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
import reflexo.workflows.api as workflows_api
from reflexo.common.config import get_settings
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import Database
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.workflows import client as temporal_client
from reflexo.workflows.automation_build import BUILD_ACTIVITIES, AutomationBuildWorkflow
from tests.integration.test_ask_owner import bakery, owner_messages, owner_says, q
from tests.integration.test_customer_desk import FALLBACK, PRIMARY
from tests.integration.test_imports import owner_conversation

pytestmark = pytest.mark.integration
DEMO_SENTENCE = (
    "Ek din mein 8 se zyada cake orders mat lena, COD orders confirm karwa lena, aur delivery se ek din pehle "
    "customer ko reminder bhejna"
)


@pytest.fixture(autouse=True)
def _own_task_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_TEMPORAL_TASK_QUEUE", f"test-{new_id().hex[-10:]}")
    monkeypatch.setenv("REFLEXO_DEMO_TIME_SCALE", str(60 / 86400))
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
    automations.register(reg)
    return reg


@pytest.fixture
async def worker() -> AsyncIterator[None]:
    try:
        c = await temporal_client.client()
    except Exception as exc:  # pragma: no cover - environment
        pytest.skip(f"Temporal not reachable: {exc}")
    async with Worker(
        c, task_queue=temporal_client.task_queue(), workflows=[AutomationBuildWorkflow], activities=BUILD_ACTIVITIES
    ):
        yield


def builder_returns(fake: agent.FakeProvider, *drafts: list[dict[str, Any]]) -> list[str]:
    """The builder model returns these drafts in order (last one repeats); returns the prompts it saw."""
    prompts: list[str] = []

    def script(a: agent.Attempt) -> agent.FakeReply:
        prompts.append(a.messages[-1].content or "")
        return agent.FakeReply(text=json.dumps({"automations": drafts[min(len(prompts), len(drafts)) - 1]}))

    fake.script(PRIMARY, script)
    fake.script(FALLBACK, script)
    return prompts


async def last_owner_text(db: Database, shop: Any, contains: str, within_s: float = 30) -> str:
    deadline = asyncio.get_running_loop().time() + within_s
    while True:
        for m in reversed((await owner_messages(db, shop))[-3:]):
            if contains in m.get("text", ""):
                return str(m["text"])
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"owner never got {contains!r}")
        await asyncio.sleep(0.3)


async def start(registry: Registry, db: Database, shop: Any, description: str) -> None:
    conv = await owner_conversation(registry, db, shop)
    await registry.require(WorkflowPort).start_automation_build(  # type: ignore[type-abstract]
        shop.bid, member_id=shop.owner, description=description, reply_conversation_id=conv, language="hi-Latn"
    )


async def statuses(db: Database, shop: Any) -> dict[str, str]:
    rows = await q(db, "SELECT name, status FROM automations WHERE business_id = :b", b=shop.bid)
    return {r.name: str(r.status) for r in rows}


async def test_demo_sentence_builds_three_rules_and_yes_activates(
    registry: Registry, db: Database, fake: agent.FakeProvider, worker: None
) -> None:
    shop = await bakery(db)
    reminder_without_template = copy.deepcopy(automations.demo.DELIVERY_REMINDER)
    del reminder_without_template["steps"][1]["template_fallback"]  # resolved deterministically
    builder_returns(
        fake, [automations.demo.DAILY_CAKE_LIMIT, automations.demo.COD_CONFIRMATION, reminder_without_template]
    )
    await start(registry, db, shop, DEMO_SENTENCE)

    text = await last_owner_text(db, shop, "Samajh gayi — ye 3 rules lagaungi")
    assert "maximum <b>8 cake</b> orders" in text and "2 ghante tak customer ke reply" in text  # Telegram HTML
    assert "delivery se ek din pehle 10:00 baje" in text and "template 'order_update'" in text
    assert "⏱ Demo mode: 1 din = 60 sec." in text
    assert await statuses(db, shop) == {
        "Daily cake limit": "draft",
        "COD confirmation": "draft",
        "Delivery reminder": "draft",
    }
    assert not await automations.active_versions(shop.bid, executor="guard")  # nothing runs before YES

    code = re.search(r"YES (\d{4})", text).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    await last_owner_text(db, shop, "Active ✅")
    assert set((await statuses(db, shop)).values()) == {"active"}
    assert [v.name for v in await automations.active_versions(shop.bid, executor="guard")] == ["Daily cake limit"]


async def test_invalid_draft_is_repaired_with_the_validator_feedback(
    registry: Registry, db: Database, fake: agent.FakeProvider, worker: None
) -> None:
    shop = await bakery(db)
    bad = copy.deepcopy(automations.demo.COD_CONFIRMATION)
    bad["steps"][0]["next"] = "nowhere"
    prompts = builder_returns(fake, [bad], [automations.demo.COD_CONFIRMATION])
    await start(registry, db, shop, "COD orders confirm karwa lena")
    await last_owner_text(db, shop, "Samajh gayi — ye 1 rule lagaungi")
    assert len(prompts) == 2 and "IT FAILED VALIDATION" in prompts[1] and "unknown step 'nowhere'" in prompts[1]


async def test_no_archives_and_unfixable_instruction_is_explained(
    registry: Registry, db: Database, fake: agent.FakeProvider, worker: None
) -> None:
    shop = await bakery(db)
    builder_returns(fake, [automations.demo.OUT_FOR_DELIVERY])
    await start(registry, db, shop, "delivery nikalte hi customer ko batana")
    text = await last_owner_text(db, shop, "Samajh gayi")
    code = re.search(r"YES (\d{4})", text).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"NO {code}")
    await last_owner_text(db, shop, "Theek hai, nahi lagaya")
    assert await statuses(db, shop) == {"Out for delivery update": "archived"}

    never_valid = copy.deepcopy(automations.demo.COD_CONFIRMATION)
    never_valid["steps"][0]["text"] = "{{ order.nope }}"
    prompts = builder_returns(fake, [never_valid])
    await start(registry, db, shop, "kuch bhi")
    await last_owner_text(db, shop, "samajh nahi paayi")
    assert len(prompts) == 3  # first draft + 2 repair rounds, then give up politely
