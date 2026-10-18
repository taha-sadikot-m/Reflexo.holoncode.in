"""Automations running (CHK-F08-4; POC_06 §5, §7) on the real local Temporal server with real commerce events.

Instant rules run once per event; the durable COD confirmation consumes the customer's "haan" (the AI does not also
reply) or notifies the owner at the (demo-scaled) timeout; the delivery reminder fast-forwards with skip_wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.worker import Worker

import reflexo.agent.api as agent
import reflexo.automations.api as automations
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
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
from reflexo.workflows.automation_run import RUN_ACTIVITIES, AutomationRunWorkflow
from tests.integration.test_commerce import FakeRazorpay, q, ready_order, run_jobs, shop_with_customer
from tests.integration.test_gateway import deliver, wa_message

pytestmark = pytest.mark.integration
CUSTOMER_WA = "919811122233"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_TEMPORAL_TASK_QUEUE", f"test-{new_id().hex[-10:]}")
    monkeypatch.setenv("REFLEXO_DEMO_TIME_SCALE", str(60 / 86400))  # 1 day = 60 s → PT2H = 5 s
    monkeypatch.setenv("REFLEXO_DEMO_RESPONSE_WINDOW_MIN_S", "0")  # tests here exercise the bare scaled timeout
    get_settings.cache_clear()
    temporal_client._client = None
    yield
    temporal_client._client = None
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def rzp(monkeypatch: pytest.MonkeyPatch) -> FakeRazorpay:
    client = FakeRazorpay()
    monkeypatch.setattr(integrations, "_client", client)
    return client


@pytest.fixture
def registry(rzp: FakeRazorpay) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=agent.FakeProvider())
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
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
        c, task_queue=temporal_client.task_queue(), workflows=[AutomationRunWorkflow], activities=RUN_ACTIVITIES
    ):
        yield


async def cod_shop(db: Database) -> dict[str, Any]:
    shop = await shop_with_customer(db)
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""UPDATE businesses SET settings = settings || '{"cod_rule": {"enabled": true,
                    "max_total_paise": 500000}}'::jsonb WHERE id = :b"""),
            {"b": shop["bid"]},
        )
        await s.execute(  # the seeded customer is who messages from this WhatsApp number
            text("""INSERT INTO identity_map (id, business_id, channel, external_id, customer_id, display_name)
                    VALUES (:id, :b, 'whatsapp', :e, :c, 'Rahul')"""),
            {"id": new_id(), "b": shop["bid"], "e": f"+{CUSTOMER_WA}", "c": shop["cust"]},
        )
        shop["pnid"] = (
            await s.execute(
                text("""SELECT external_account_id FROM channel_connections
                        WHERE business_id = :b AND channel = 'whatsapp'"""),
                {"b": shop["bid"]},
            )
        ).scalar_one()
    return shop


async def confirmed_cod_order(registry: Registry, shop: dict[str, Any]) -> Any:
    order = await ready_order(shop)
    result = await commerce.confirm_order(shop["bid"], order.order_id, "cod", idem=f"cod-{new_id().hex[:6]}")
    assert result.status == "confirmed"
    assert await run_jobs(registry, "automations.route") >= 1  # order.confirmed → router
    return order


async def customer_texts(db: Database, shop: dict[str, Any]) -> list[str]:
    rows = await q(
        db,
        "SELECT rendered_payload FROM outbound_messages WHERE conversation_id = :c ORDER BY created_at",
        c=shop["conv"],
    )
    return [r.rendered_payload.get("text", {}).get("body", "") for r in rows]


async def eventually(check: Any, within_s: float = 30) -> Any:
    deadline = asyncio.get_running_loop().time() + within_s
    while True:
        if result := await check():
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met: {check.__name__}")
        await asyncio.sleep(0.3)


async def test_instant_rule_runs_once_per_event(registry: Registry, db: Database, worker: None) -> None:
    shop = await cod_shop(db)
    thanks = {
        "schema_version": 1,
        "name": "Order thanks",
        "trigger": {"type": "order_confirmed"},
        "instance_key": "{{ order.id }}",
        "steps": [
            {
                "id": "s",
                "type": "send",
                "to": "customer",
                "text": "Shukriya! Order {{ order.number }} mil gaya 🎂",
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ],
    }
    await automations.install(shop["bid"], thanks, description_nl="order aate hi thanks bolna")
    await confirmed_cod_order(registry, shop)
    assert await run_jobs(registry, "automations.instant_step") == 1
    assert "Shukriya! Order MB-1001 mil gaya 🎂" in await customer_texts(db, shop)
    [run] = await q(db, "SELECT status FROM automation_runs WHERE business_id = :b", b=shop["bid"])
    assert str(run.status) == "completed"


@pytest.mark.check("CHK-ARCH-5")
async def test_an_automation_send_is_decided_before_it_goes_out(registry: Registry, db: Database) -> None:
    from tests.integration.test_policy import undecided_effects

    shop = await cod_shop(db)
    thanks = {
        "schema_version": 1,
        "name": "Order thanks",
        "trigger": {"type": "order_confirmed"},
        "instance_key": "{{ order.id }}",
        "steps": [
            {"id": "s", "type": "send", "to": "customer", "text": "Shukriya! {{ order.number }}", "next": "e"},
            {"id": "e", "type": "end"},
        ],
    }
    await automations.install(shop["bid"], thanks, description_nl="order aate hi thanks bolna")
    await confirmed_cod_order(registry, shop)
    assert await run_jobs(registry, "automations.instant_step") == 1
    assert "Shukriya! MB-1001" in await customer_texts(db, shop)
    assert await undecided_effects(db, shop["bid"]) == []
    [decision] = await q(
        db, "SELECT attrs FROM pipeline_events WHERE business_id = :b AND stage = 'decision'", b=shop["bid"]
    )
    assert (decision.attrs["capability"], decision.attrs["requested_by"]) == ("message.send", "automation")


async def test_cod_confirmation_consumes_the_customers_haan(registry: Registry, db: Database, worker: None) -> None:
    shop = await cod_shop(db)
    await automations.install(shop["bid"], automations.demo.COD_CONFIRMATION, description_nl="COD confirm karwana")
    await confirmed_cod_order(registry, shop)

    async def asked() -> bool:
        return any("Please reply YES to confirm order" in t for t in await customer_texts(db, shop))

    await eventually(asked)

    async def waiting() -> bool:
        return bool(
            await q(db, "SELECT id FROM automation_waits WHERE business_id = :b AND status = 'waiting'", b=shop["bid"])
        )

    await eventually(waiting)
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(shop["pnid"], CUSTOMER_WA, f"wamid.{new_id()}", "haan ji"),
        shop["pnid"],
    )

    async def thanked() -> bool:
        return "Thank you! Order confirmed ✅" in await customer_texts(db, shop)

    await eventually(thanked)
    turns = await q(db, "SELECT id FROM conversation_turns WHERE conversation_id = :c", c=shop["conv"])
    assert turns == []  # consumed: the AI agent did not also reply to "haan ji"


async def test_cod_confirmation_without_reply_tells_the_owner(registry: Registry, db: Database, worker: None) -> None:
    shop = await cod_shop(db)
    await automations.install(shop["bid"], automations.demo.COD_CONFIRMATION, description_nl="COD confirm karwana")
    await confirmed_cod_order(registry, shop)

    async def owner_told() -> bool:  # PT2H at 1 day = 60 s → 5 s
        rows = await q(
            db, "SELECT payload FROM owner_notifications WHERE business_id = :b AND kind = 'automation'", b=shop["bid"]
        )
        return any("was not confirmed within 2 hours" in r.payload.get("text", "") for r in rows)

    await eventually(owner_told, within_s=40)


async def test_delivery_reminder_fast_forwards(registry: Registry, db: Database, worker: None) -> None:
    shop = await cod_shop(db)
    installed = await automations.install(shop["bid"], automations.demo.DELIVERY_REMINDER, description_nl="reminder")
    order = await confirmed_cod_order(registry, shop)
    await asyncio.sleep(1.0)  # let the workflow reach its wait
    delivered = await registry.require(WorkflowPort).signal(  # type: ignore[type-abstract]
        f"auto-{installed.version_id}-{order.order_id}", "skip_wait", {}
    )
    assert delivered

    async def reminded() -> bool:
        return any(
            "Reminder: your order MB-1001 will be delivered tomorrow" in t for t in await customer_texts(db, shop)
        )

    await eventually(reminded)


async def test_a_late_customer_reply_still_counts_within_the_response_floor(
    registry: Registry, db: Database, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 12 Oct: PT2H at demo scale was 5 s, so the owner was told "not confirmed" before the customer could
    read the message, and the customer's "ok" 44 s later went nowhere. Response windows have a floor."""
    monkeypatch.setenv("REFLEXO_DEMO_RESPONSE_WINDOW_MIN_S", "20")
    get_settings.cache_clear()
    shop = await cod_shop(db)
    await automations.install(shop["bid"], automations.demo.COD_CONFIRMATION, description_nl="COD confirm karwana")
    await confirmed_cod_order(registry, shop)

    async def waiting() -> bool:
        return bool(
            await q(db, "SELECT id FROM automation_waits WHERE business_id = :b AND status = 'waiting'", b=shop["bid"])
        )

    await eventually(waiting)
    await asyncio.sleep(8)  # past the bare scaled window (5 s)
    await deliver(
        registry, db, "meta_whatsapp", wa_message(shop["pnid"], CUSTOMER_WA, f"wamid.{new_id()}", "ok"), shop["pnid"]
    )

    async def thanked() -> bool:
        return "Thank you! Order confirmed ✅" in await customer_texts(db, shop)

    await eventually(thanked)
    told = await q(
        db, "SELECT id FROM owner_notifications WHERE business_id = :b AND kind = 'automation'", b=shop["bid"]
    )
    assert told == []


async def test_cancelling_an_order_ends_its_runs_and_frees_capacity(
    registry: Registry, db: Database, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 12 Oct: after "I want to cancel", the COD wait still told the owner "not confirmed" and the reminder
    still fired. A cancelled order ends every run about it — no automation needs its own "on cancel" branch."""
    monkeypatch.setenv("REFLEXO_DEMO_RESPONSE_WINDOW_MIN_S", "60")  # the wait outlives the test unless cancelled
    get_settings.cache_clear()
    shop = await cod_shop(db)
    await automations.install(shop["bid"], automations.demo.DAILY_CAKE_LIMIT, description_nl="8 cake roz")
    await automations.install(shop["bid"], automations.demo.COD_CONFIRMATION, description_nl="COD confirm karwana")
    order = await confirmed_cod_order(registry, shop)

    async def waiting() -> bool:
        return bool(
            await q(
                db,
                "SELECT id FROM automation_waits WHERE run_id IN (SELECT id FROM automation_runs "
                "WHERE instance_key = :k) AND status = 'waiting'",
                k=str(order.order_id),
            )
        )

    await eventually(waiting)
    [used] = await q(db, "SELECT value FROM automation_counters WHERE business_id = :b", b=shop["bid"])
    assert used.value == 1

    result = await commerce.cancel_order(shop["bid"], order.order_id, by="customer", reason="plans changed")
    assert result.status == "cancelled" and result.refund_due_paise == 0
    assert await run_jobs(registry, "automations.route") >= 1  # order.cancelled → router ends the runs

    runs = await q(db, "SELECT status FROM automation_runs WHERE instance_key = :k", k=str(order.order_id))
    assert runs and {str(r.status) for r in runs} <= {"cancelled", "completed"}
    assert not await waiting()
    [freed] = await q(db, "SELECT value FROM automation_counters WHERE business_id = :b", b=shop["bid"])
    assert freed.value == 0

    async def workflow_cancelled() -> bool:
        handle = (await temporal_client.client()).get_workflow_handle(
            f"auto-{
                (
                    await q(
                        db,
                        'SELECT automation_version_id FROM automation_runs WHERE instance_key = :k AND '
                        'workflow_id IS NOT NULL',
                        k=str(order.order_id),
                    )
                )[0].automation_version_id
            }-{order.order_id}"
        )
        return (await handle.describe()).status.name in ("CANCELED", "COMPLETED")

    await eventually(workflow_cancelled)
    kinds = [r.kind for r in await q(db, "SELECT kind FROM owner_notifications WHERE business_id = :b", b=shop["bid"])]
    assert "order_cancelled" in kinds and "automation" not in kinds  # told about the cancel, never "not confirmed"
    again = await commerce.cancel_order(shop["bid"], order.order_id, by="customer")
    assert again.status == "already_cancelled"


async def test_the_agent_sees_the_shops_procedures_and_what_they_wait_for(
    registry: Registry, db: Database, worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 12 Oct: the COD rule asked "reply YES to confirm" while the agent said "confirmed" — it could not see
    the owner's procedure. Now its context shows the procedure and the open wait for this customer."""
    from reflexo.agent.context import prepare
    from tests.integration.test_ask_owner import claim_pending

    monkeypatch.setenv("REFLEXO_DEMO_RESPONSE_WINDOW_MIN_S", "60")
    get_settings.cache_clear()
    shop = await cod_shop(db)
    await automations.install(shop["bid"], automations.demo.COD_CONFIRMATION, description_nl="COD confirm karwana")
    await confirmed_cod_order(registry, shop)

    async def waiting() -> bool:
        return bool(
            await q(db, "SELECT id FROM automation_waits WHERE business_id = :b AND status = 'waiting'", b=shop["bid"])
        )

    await eventually(waiting)
    question = wa_message(shop["pnid"], CUSTOMER_WA, f"wamid.{new_id()}", "did my order go through?")
    await deliver(registry, db, "meta_whatsapp", question, shop["pnid"])  # not a YES: the agent answers it
    turn = await claim_pending(registry, db, shop["conv"])
    prepared = await prepare(turn.ref)
    assert "# HOW THIS SHOP WORKS" in prepared.dynamic and "COD confirmation" in prepared.dynamic
    assert "# OPEN WITH THIS CUSTOMER" in prepared.dynamic
    assert "is waiting for this customer's reply" in prepared.dynamic
