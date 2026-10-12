"""ImportWorkflow end to end on the real (local) Temporal server (F06/F07, POC_05 §3; demo segment 6–7).

Owner Excel → summary with code → YES on Telegram → workflow applies the change set → catalogue updated. Plus
cancel and an unreadable file. Each test runs its own worker on its own task queue.
"""

from __future__ import annotations

import asyncio
import io
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
from openpyxl import Workbook
from sqlalchemy import text
from temporalio.worker import Worker

import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
import reflexo.workflows.api as workflows_api
from reflexo.common.config import get_settings
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.workflows import client as temporal_client
from reflexo.workflows.import_activities import ACTIVITIES
from reflexo.workflows.imports import ImportWorkflow
from tests.integration.test_ask_owner import Bakery, bakery, owner_messages, owner_says, q

pytestmark = pytest.mark.integration
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _own_task_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_TEMPORAL_TASK_QUEUE", f"test-{new_id().hex[-10:]}")
    get_settings.cache_clear()
    temporal_client._client = None  # a client is bound to the event loop that created it
    yield
    temporal_client._client = None
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=agent.FakeProvider())
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
    async with Worker(c, task_queue=temporal_client.task_queue(), workflows=[ImportWorkflow], activities=ACTIVITIES):
        yield


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def owner_file(db: Database, shop: Bakery, content: bytes, mime: str, filename: str, kind: str) -> UUID:
    media = await gateway_api.store_media(shop.bid, content, mime, source="inbound", provider_media_id=None)
    fid = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO owner_files (id, business_id, member_id, media_object_id, filename, kind, received_via)
                    VALUES (:id, :b, :m, :mo, :fn, CAST(:k AS file_kind), 'whatsapp')"""),
            {"id": fid, "b": shop.bid, "m": shop.owner, "mo": media, "fn": filename, "k": kind},
        )
    return fid


async def owner_conversation(registry: Registry, db: Database, shop: Bakery) -> UUID:
    await owner_says(registry, db, shop, "/help")  # creates the owner's Telegram conversation
    [conv] = await q(
        db,
        "SELECT id FROM conversations WHERE channel_connection_id = :tg AND member_id = :m",
        tg=shop.tg,
        m=shop.owner,
    )
    return UUID(str(conv.id))


async def wait_for(db: Database, run_id: UUID, statuses: set[str], within_s: float = 30) -> Any:
    deadline = asyncio.get_running_loop().time() + within_s
    while True:
        rows = await q(db, "SELECT status, stats, error FROM import_runs WHERE id = :r", r=run_id)
        if rows and str(rows[0].status) in statuses:
            return rows[0]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"import {run_id} still {rows[0].status if rows else 'missing'}")
        await asyncio.sleep(0.3)


async def owner_message_with(db: Database, shop: Bakery, needle: str, within_s: float = 15) -> dict[str, Any]:
    """The latest owner message containing `needle`. A workflow's status changes before the activity that sends
    its message runs, so reading "the last message" right after a status change races under load."""
    deadline = asyncio.get_running_loop().time() + within_s
    while True:
        for m in reversed(await owner_messages(db, shop)):
            if needle in m.get("text", ""):
                return m
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no owner message with {needle!r}")
        await asyncio.sleep(0.2)


async def price_of(db: Database, shop: Bakery, offering: str, variant: str) -> int:
    [row] = await q(
        db,
        """SELECT v.price_paise FROM offering_variants v JOIN offerings o ON o.id = v.offering_id
           WHERE o.business_id = :b AND o.name = :o AND v.name = :v""",
        b=shop.bid,
        o=offering,
        v=variant,
    )
    return int(row.price_paise)


PRICE_SHEET = [
    ["Item", "Size", "Price"],
    ["Chocolate Truffle Cake", "1 kg", 999],
    ["Chocolate Truffle Cake", "500 g", 550],
    ["Lemon Tart", None, 380],
    ["Custom Photo Cake", None, None],
]


async def test_excel_import_reviewed_and_applied(registry: Registry, db: Database, worker: None) -> None:
    shop = await bakery(db)
    conv = await owner_conversation(registry, db, shop)
    fid = await owner_file(db, shop, _xlsx(PRICE_SHEET), XLSX, "prices_oct.xlsx", "spreadsheet")
    port = registry.require(WorkflowPort)  # type: ignore[type-abstract]
    run_id = await port.start_import(
        shop.bid, fid, mode="catalog_update", member_id=shop.owner, reply_conversation_id=conv
    )

    review = await wait_for(db, run_id, {"review", "failed"})
    assert str(review.status) == "review", review.error
    assert review.stats == {"found": 3, "new": 1, "changed": 1, "unchanged": 0, "needs_review": 1}
    summary = await owner_message_with(db, shop, "prices_oct.xlsx")
    body = summary["text"]
    assert "prices_oct.xlsx" in body and "3" not in body.split("\n")[0][:5]
    assert "Chocolate Truffle Cake 1 kg price: ₹950 → ₹999" in body and "Lemon Tart — ₹380" in body
    assert "Custom Photo Cake — price not found" in body
    assert await price_of(db, shop, "Chocolate Truffle Cake", "1 kg") == 95000  # nothing applied yet

    code = re.search(r"YES (\d{4})", body).group(1)  # type: ignore[union-attr]
    await owner_says(registry, db, shop, f"YES {code}")
    done = await wait_for(db, run_id, {"committed", "failed"})
    assert str(done.status) == "committed", done.error
    assert await price_of(db, shop, "Chocolate Truffle Cake", "1 kg") == 99900
    assert await knowledge.resolve_offerings(shop.bid, "lemon tart")
    for _ in range(20):  # the "Done" message is sent by the workflow's last activity
        if "Done ✅ 2 changes applied" in (await owner_messages(db, shop))[-1].get("text", ""):
            break
        await asyncio.sleep(0.2)
    assert "Done ✅ 2 changes applied" in (await owner_messages(db, shop))[-1]["text"]

    await owner_says(registry, db, shop, "/undo")  # the import is one undoable change set
    assert await price_of(db, shop, "Chocolate Truffle Cake", "1 kg") == 95000


async def test_cancel_button_applies_nothing(registry: Registry, db: Database, worker: None) -> None:
    shop = await bakery(db)
    conv = await owner_conversation(registry, db, shop)
    fid = await owner_file(db, shop, _xlsx(PRICE_SHEET), XLSX, "prices.xlsx", "spreadsheet")
    run_id = await registry.require(WorkflowPort).start_import(  # type: ignore[type-abstract]
        shop.bid, fid, mode="catalog_update", member_id=shop.owner, reply_conversation_id=conv
    )
    await wait_for(db, run_id, {"review"})
    summary = await owner_message_with(db, shop, "prices.xlsx")
    cancel = summary["reply_markup"]["inline_keyboard"][1][0]["callback_data"].removeprefix("o:")
    await owner_says(registry, db, shop, callback=cancel)
    await wait_for(db, run_id, {"cancelled"})
    assert await price_of(db, shop, "Chocolate Truffle Cake", "1 kg") == 95000


async def test_unreadable_file_fails_with_owner_message(registry: Registry, db: Database, worker: None) -> None:
    shop = await bakery(db)
    conv = await owner_conversation(registry, db, shop)
    fid = await owner_file(db, shop, b"not an excel file", XLSX, "broken.xlsx", "spreadsheet")
    run_id = await registry.require(WorkflowPort).start_import(  # type: ignore[type-abstract]
        shop.bid, fid, mode="catalog_update", member_id=shop.owner, reply_conversation_id=conv
    )
    failed = await wait_for(db, run_id, {"failed"})
    assert "password" in failed.error
    for _ in range(20):
        if "Couldn't import this file" in (await owner_messages(db, shop))[-1].get("text", ""):
            break
        await asyncio.sleep(0.2)
    assert "Couldn't import this file" in (await owner_messages(db, shop))[-1]["text"]
