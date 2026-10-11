"""Commerce on a real database (POC_05 §4; CHK-F03-1..7): orders, details, COD rule, guards, Razorpay links,
signed webhooks → paid → confirmed → customer summary + owner ping, reconciliation, order numbers, tools, gate."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner_api
from reflexo.api.main import app
from reflexo.common.contracts import GuardResult, OrderGuardPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.integrations.razorpay import PaymentLink
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_customer_desk import SEED, make_bakery

pytestmark = pytest.mark.integration
WEBHOOK_SECRET = "test-rzp-webhook-secret"


class FakeRazorpay:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.remote_status = "created"
        self.tag = new_id().hex[-8:]  # provider ids are globally unique, also across tests on one database

    configured = True

    async def create_payment_link(self, **kw: Any) -> PaymentLink:
        for c in self.created:  # Razorpay: reference_id is unique -> same link
            if c["reference_id"] == kw["reference_id"]:
                return PaymentLink(
                    id=c["id"],
                    short_url=c["url"],
                    status="created",
                    amount=c["amount_paise"],
                    reference_id=c["reference_id"],
                )
        n = len(self.created) + 1
        self.created.append({**kw, "id": f"plink_{self.tag}_{n}", "url": f"https://rzp.io/rzp/{self.tag}{n}"})
        return PaymentLink(
            id=f"plink_{self.tag}_{n}",
            short_url=f"https://rzp.io/rzp/{self.tag}{n}",
            status="created",
            amount=kw["amount_paise"],
            reference_id=kw["reference_id"],
        )

    async def fetch_payment_link(self, link_id: str) -> PaymentLink:
        c = next(c for c in self.created if c["id"] == link_id)
        return PaymentLink(
            id=link_id,
            short_url=c["url"],
            status=self.remote_status,
            amount=c["amount_paise"],
            reference_id=c["reference_id"],
            payment_id="pay_reconciled" if self.remote_status == "paid" else None,
        )


class Guard:
    def __init__(self) -> None:
        self.deny = False
        self.released: list[str] = []

    async def check(self, business_id: UUID, order: dict[str, Any]) -> GuardResult:
        if self.deny:
            return GuardResult(False, "15 Oct is fully booked", ["2026-10-16", "2026-10-17"], code="CAPACITY_FULL")
        return GuardResult(True)

    async def release(self, business_id: UUID, order_id: UUID, reason: str) -> None:
        self.released.append(reason)


@pytest.fixture
def rzp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRazorpay]:
    from reflexo.common.config import get_settings

    fake = FakeRazorpay()
    monkeypatch.setattr(integrations, "_client", fake)
    monkeypatch.setenv("REFLEXO_RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    yield fake
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def guard() -> Guard:
    return Guard()


@pytest.fixture
def registry(guard: Guard) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=agent.FakeProvider())
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    owner_api.register(reg)
    reg.provide(OrderGuardPort, guard)  # type: ignore[type-abstract]
    return reg


async def shop_with_customer(db: Database) -> dict[str, Any]:
    bid, _ = await make_bakery(db)
    cust, conv, owner, tg = new_id(), new_id(), new_id(), new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        for schema in SEED["order_detail_schemas"]:
            await s.execute(
                text("""INSERT INTO order_detail_schemas (id, business_id, name, applies_to, fields)
                                    VALUES (:id, :b, :n, CAST(:a AS jsonb), CAST(:f AS jsonb))"""),
                {
                    "id": new_id(),
                    "b": bid,
                    "n": schema["name"],
                    "a": json.dumps(schema["applies_to"]),
                    "f": json.dumps(schema["fields"]),
                },
            )
        await s.execute(
            text("INSERT INTO business_counters (business_id, name, value) VALUES (:b, 'order_number', 1000)"),
            {"b": bid},
        )
        await s.execute(
            text("""INSERT INTO customers (id, business_id, display_name, phone_e164)
                                VALUES (:id, :b, 'Rahul', '+919811122233')"""),
            {"id": cust, "b": bid},
        )
        wa = (
            await s.execute(text("SELECT id FROM channel_connections WHERE business_id = :b"), {"b": bid})
        ).scalar_one()
        await s.execute(
            text("""INSERT INTO conversations (id, business_id, channel, channel_connection_id,
                                participant_role, customer_id, thread_key, last_inbound_at)
                                VALUES (:id, :b, 'whatsapp', :c, 'customer', :cu, '919811122233', now())"""),
            {"id": conv, "b": bid, "c": wa, "cu": cust},
        )
        # the owner, reachable on Telegram
        await s.execute(
            text("INSERT INTO team_members (id, business_id, display_name, role) VALUES (:id, :b, 'Meera', 'owner')"),
            {"id": owner, "b": bid},
        )
        await s.execute(
            text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                display_name, credential_ref)
                                VALUES (:id, :b, 'telegram', 'telegram', :e, 'TG', 'REFLEXO_TELEGRAM')"""),
            {"id": tg, "b": bid, "e": f"tg-{tg.hex[-8:]}"},
        )
        await s.execute(
            text("""INSERT INTO owner_channel_bindings (id, business_id, member_id, channel, external_id, verified_at)
                                VALUES (:id, :b, :m, 'telegram', '555001', now())"""),
            {"id": new_id(), "b": bid, "m": owner},
        )
    cards = {c.name: c for c in await knowledge.search_catalog(bid, "cake", k=10)}
    return {"bid": bid, "cust": cust, "conv": conv, "tg": tg, "cards": cards}


def variant(card: Any, name: str) -> UUID:
    return next(v.variant_id for v in card.variants if v.name == name)


def future(days: int = 3) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def ready_order(
    shop: dict[str, Any],
    *,
    cake: str = "Chocolate Truffle Cake",
    size: str = "1 kg",
    qty: int = 1,
    address: str = "Kothrud 411038",
    eggless: str = "yes",
) -> commerce.OrderView:
    card = shop["cards"][cake]
    v = await commerce.start_order(
        shop["bid"],
        shop["cust"],
        shop["conv"],
        [commerce.ItemRef(offering_id=card.offering_id, variant_id=variant(card, size), quantity=qty)],
    )
    return await commerce.update_order(
        shop["bid"],
        v.order_id,
        details={"eggless": eggless, "delivery_date": future(), "delivery_slot": "4pm-8pm", "address": address},
    )


# ---------------------------------------------------------------- orders and details (CHK-F03-1)


async def test_order_lifecycle_details_and_totals(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    truffle = shop["cards"]["Chocolate Truffle Cake"]
    with pytest.raises(commerce.CommerceError) as err:  # several sizes, none chosen
        await commerce.start_order(
            shop["bid"], shop["cust"], shop["conv"], [commerce.ItemRef(offering_id=truffle.offering_id)]
        )
    assert err.value.code == "VALIDATION_FAILED" and "500 g (₹550) or 1 kg (₹950)" in (err.value.guidance or "")

    v = await commerce.start_order(
        shop["bid"],
        shop["cust"],
        shop["conv"],
        [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=variant(truffle, "1 kg"))],
    )
    assert v.order_number == "MB-1001" and v.subtotal_paise == 95000 and v.status == "draft"
    assert set(v.missing_details) >= {"eggless", "delivery_date", "delivery_slot", "address"}

    # one call, mixed: valid details are kept, invalid ones come back with guidance (live bug: all were lost)
    mixed = await commerce.update_order(
        shop["bid"],
        v.order_id,
        details={
            "eggless": "yes",
            "delivery_date": date.today().isoformat(),
            "delivery_slot": "midnight",
            "address": "Hinjewadi",
        },
    )
    rejected = {e["key"]: e for e in mixed.detail_errors}
    assert set(rejected) == {"delivery_date", "delivery_slot", "address"}
    guidance = rejected["delivery_date"]["guidance"]  # ISO + year: the model must not guess (live bug: 2023)
    assert re.search(rf"\({date.today().year}-\d\d-\d\d\)", guidance), guidance
    assert "10am-1pm" in rejected["delivery_slot"]["guidance"] and rejected["address"]["code"] == "NOT_SERVICEABLE"
    assert "eggless" not in mixed.missing_details and "not_accepted" in mixed.tool_view()
    past = await commerce.update_order(shop["bid"], v.order_id, details={"delivery_date": "2023-10-13"})
    assert past.detail_errors[0]["reason"] == "2023-10-13 is in the past"
    evening = await commerce.update_order(shop["bid"], v.order_id, details={"delivery_slot": "shaam"})
    assert evening.delivery_slot == "4pm-8pm" and evening.detail_errors == []  # "evening"/"shaam" -> the one slot

    v = await commerce.update_order(
        shop["bid"],
        v.order_id,
        details={
            "eggless": "haan",
            "delivery_date": future(),
            "delivery_slot": "4 pm-8 pm",
            "address": "Kothrud 411038",
        },
    )
    assert v.missing_details == [] and v.delivery_slot == "4pm-8pm"
    assert [ln.name for ln in v.lines] == ["Chocolate Truffle Cake 1 kg", "Eggless (Chocolate Truffle Cake)"]
    assert (v.subtotal_paise, v.delivery_fee_paise, v.total_paise) == (100000, 5000, 105000)  # eggless +₹50, fee ₹50

    two = await commerce.update_order(
        shop["bid"],
        v.order_id,
        add_items=[commerce.ItemRef(offering_id=truffle.offering_id, variant_id=variant(truffle, "1 kg"), quantity=2)],
    )
    assert two.subtotal_paise == 200000 and two.delivery_fee_paise == 0  # free delivery above ₹1,500
    again = await commerce.start_order(
        shop["bid"],
        shop["cust"],
        shop["conv"],
        [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=variant(truffle, "500 g"))],
    )
    assert again.order_id == v.order_id  # one open draft per customer


# ---------------------------------------------------------------- COD rule and guards (CHK-F03-5)


async def test_cod_rule_and_guard_decline(registry: Registry, db: Database, rzp: FakeRazorpay, guard: Guard) -> None:
    shop = await shop_with_customer(db)
    big = await ready_order(shop)  # ₹1,050
    no_rule = await commerce.confirm_order(shop["bid"], big.order_id, "cod", idem="t1")
    assert (no_rule.status, no_rule.decline_code) == ("declined", "COD_NOT_ALLOWED")

    async with db.service_session(ServiceReason.ADMIN) as s:  # owner sets "COD only under ₹1,000"
        await s.execute(
            text("""UPDATE businesses SET settings = settings || '{"cod_rule": {"enabled": true,
                                "max_total_paise": 100000}}'::jsonb WHERE id = :b"""),
            {"b": shop["bid"]},
        )
    over = await commerce.confirm_order(shop["bid"], big.order_id, "cod", idem="t2")
    assert (
        over.status == "declined" and "₹1,000" in (over.decline_reason or "") and over.alternatives == ["razorpay_link"]
    )
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("DELETE FROM order_items WHERE order_id = :o"), {"o": big.order_id})
    small = await ready_order(shop, cake="Pineapple Cake", eggless="no")  # ₹750 + ₹50 fee = ₹800
    assert small.order_id == big.order_id and small.total_paise == 80000
    cod = await commerce.confirm_order(shop["bid"], small.order_id, "cod", idem="t3")
    assert (cod.status, cod.order.payment_status) == ("confirmed", "cod_pending")
    [ev] = await q(
        db, "SELECT payload FROM domain_events WHERE type = 'order.confirmed' AND business_id = :b", b=shop["bid"]
    )
    assert ev.payload["payment_method"] == "cod"
    [note] = await q(
        db, "SELECT kind, status, delivered_via FROM owner_notifications WHERE business_id = :b", b=shop["bid"]
    )
    assert (note.kind, str(note.status), str(note.delivered_via)) == ("order_cod", "sent", "telegram")
    [ping] = await q(
        db,
        """SELECT o.rendered_payload FROM outbound_messages o JOIN conversations c ON c.id = o.conversation_id
                            WHERE c.channel_connection_id = :tg""",
        tg=shop["tg"],
    )
    assert "New COD order MB-1001" in ping.rendered_payload["text"] and ping.rendered_payload["chat_id"] == "555001"

    other = await shop_with_customer(db)
    o2 = await ready_order(other)
    guard.deny = True
    denied = await commerce.confirm_order(other["bid"], o2.order_id, "razorpay_link", idem="t4")
    assert (denied.status, denied.decline_code, denied.alternatives) == (
        "declined",
        "CAPACITY_FULL",
        ["2026-10-16", "2026-10-17"],
    )
    assert rzp.created == []  # no link for a declined order


# ---------------------------------------------------------------- Razorpay link + webhook (CHK-F03-2, -3, -6)


async def post_webhook(payload: dict[str, Any], *, event_id: str, secret: str = WEBHOOK_SECRET) -> httpx.Response:
    raw = json.dumps(payload).encode()
    headers = {
        "x-razorpay-signature": integrations.sign(raw, secret),
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/integrations/webhooks/razorpay", content=raw, headers=headers)


def paid_event(link_id: str, reference_id: str, amount: int) -> dict[str, Any]:
    return {
        "event": "payment_link.paid",
        "created_at": 1760000000,
        "payload": {
            "payment_link": {
                "entity": {"id": link_id, "reference_id": reference_id, "status": "paid", "amount_paid": amount}
            },
            "payment": {"entity": {"id": "pay_123", "amount": amount, "status": "captured"}},
        },
    }


async def run_jobs(registry: Registry, queue: str) -> int:
    from reflexo.common.contracts import JobHandlerRegistry, JobQueuePort

    jobs = registry.require(JobQueuePort)  # type: ignore[type-abstract]
    handler = registry.require(JobHandlerRegistry).get(queue)  # type: ignore[type-abstract]
    claimed = await jobs.claim("test", 30, 100, only=[queue])
    for j in claimed:
        await handler(j)
        await jobs.complete(j, "test")
    return len(claimed)


async def test_payment_link_webhook_confirms_order(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    order = await ready_order(shop)
    res = await commerce.confirm_order(shop["bid"], order.order_id, "razorpay_link", idem="c1")
    assert res.status == "awaiting_payment" and res.payment_url == rzp.created[0]["url"]
    assert rzp.created[0]["amount_paise"] == 105000 and rzp.created[0]["customer_contact"] == "+919811122233"
    assert rzp.created[0]["reference_id"] == str(res.payment_link_id)
    again = await commerce.confirm_order(shop["bid"], order.order_id, "razorpay_link", idem="c2")
    assert again.payment_link_id == res.payment_link_id and len(rzp.created) == 1  # asking again: same live link
    evt = f"evt_{rzp.tag}"
    [job] = await q(
        db,
        "SELECT run_after > now() AS later FROM jobs WHERE queue = 'commerce.reconcile_link' AND business_id = :b",
        b=shop["bid"],
    )
    assert job.later

    assert (
        await post_webhook(
            paid_event(rzp.created[0]["id"], str(res.payment_link_id), 105000), event_id=evt, secret="wrong"
        )
    ).status_code == 401
    ok = await post_webhook(paid_event(rzp.created[0]["id"], str(res.payment_link_id), 105000), event_id=evt)
    dup = await post_webhook(paid_event(rzp.created[0]["id"], str(res.payment_link_id), 105000), event_id=evt)
    assert ok.status_code == dup.status_code == 200
    assert await run_jobs(registry, "commerce.payment_event") == 1  # the duplicate never became a job

    [o] = await q(db, "SELECT status, payment_status, confirmed_at FROM orders WHERE id = :o", o=order.order_id)
    assert (str(o.status), str(o.payment_status)) == ("confirmed", "paid") and o.confirmed_at is not None
    [link] = await q(db, "SELECT status, provider_payment_id FROM payment_links WHERE id = :l", l=res.payment_link_id)
    assert (str(link.status), link.provider_payment_id) == ("paid", "pay_123")
    types = [
        r.type
        for r in await q(
            db, "SELECT type FROM domain_events WHERE business_id = :b ORDER BY occurred_at", b=shop["bid"]
        )
    ]
    assert types[-2:] == ["payment.captured", "order.confirmed"]
    customer = await q(
        db,
        "SELECT rendered_payload FROM outbound_messages WHERE conversation_id = :c ORDER BY created_at",
        c=shop["conv"],
    )
    texts = [c.rendered_payload.get("text", {}).get("body", "") for c in customer]
    assert texts[0].startswith("Payment received ✅") and "🧾 Order MB-1001" in texts[1] and "Total: ₹1,050" in texts[1]
    [note] = await q(db, "SELECT kind FROM owner_notifications WHERE business_id = :b", b=shop["bid"])
    assert note.kind == "order_confirmed"

    # replay of the same payment through reconciliation changes nothing
    assert (
        await commerce.apply_paid(
            shop["bid"], res.payment_link_id, payment_id="pay_123", amount_paid=105000, source="reconcile"
        )
        is False
    )


async def test_reconciliation_confirms_without_webhook(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    order = await ready_order(shop)
    res = await commerce.confirm_order(shop["bid"], order.order_id, "razorpay_link", idem="r1")
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE jobs SET run_after = now() WHERE queue = 'commerce.reconcile_link' AND business_id = :b"),
            {"b": shop["bid"]},
        )
    await run_jobs(registry, "commerce.reconcile_link")  # still unpaid -> re-scheduled
    [o] = await q(db, "SELECT status FROM orders WHERE id = :o", o=order.order_id)
    assert str(o.status) == "awaiting_payment"
    rzp.remote_status = "paid"  # the webhook was lost, but the customer paid
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text(
                "UPDATE jobs SET run_after = now() WHERE queue = 'commerce.reconcile_link' AND status = 'pending' "
                "AND business_id = :b"
            ),
            {"b": shop["bid"]},
        )
    assert await run_jobs(registry, "commerce.reconcile_link") == 1
    [o] = await q(db, "SELECT status, payment_status FROM orders WHERE id = :o", o=order.order_id)
    assert (str(o.status), str(o.payment_status)) == ("confirmed", "paid")
    assert res.payment_link_id is not None


async def test_order_numbers_are_unique_under_concurrency(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    truffle = shop["cards"]["Chocolate Truffle Cake"]
    customers = []
    async with db.service_session(ServiceReason.SEED) as s:
        for _ in range(6):
            cid = new_id()
            await s.execute(
                text("INSERT INTO customers (id, business_id) VALUES (:id, :b)"), {"id": cid, "b": shop["bid"]}
            )
            customers.append(cid)
    views = await asyncio.gather(
        *(
            commerce.start_order(
                shop["bid"],
                c,
                None,
                [commerce.ItemRef(offering_id=truffle.offering_id, variant_id=variant(truffle, "1 kg"))],
            )
            for c in customers
        )
    )
    numbers = sorted(v.order_number for v in views)
    assert numbers == [f"MB-{n}" for n in range(1001, 1007)]


# ---------------------------------------------------------------- tools and the output gate


async def test_order_tools_and_grounding(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    shop = await shop_with_customer(db)
    truffle = shop["cards"]["Chocolate Truffle Cake"]
    ctx = agent.ToolContext(
        business_id=shop["bid"],
        desk="customer",
        channel="whatsapp",
        conversation_id=shop["conv"],
        customer_id=shop["cust"],
        member_id=None,
        turn_id=new_id(),
        step=1,
    )
    bound = {"order_start", "order_update", "order_confirm", "order_status"}
    no_size = await agent.run_tool(
        ToolCall(id="1", name="order_start", args={"items": [{"offering_id": str(truffle.offering_id)}]}), ctx, bound
    )
    assert no_size["error"]["code"] == "VALIDATION_FAILED" and "Ask which size" in no_size["error"]["guidance"]
    started = await agent.run_tool(
        ToolCall(
            id="2",
            name="order_start",
            args={"items": [{"offering_id": str(truffle.offering_id), "variant_id": str(variant(truffle, "1 kg"))}]},
        ),
        ctx.__class__(**{**ctx.__dict__, "step": 2}),
        bound,
    )
    data = started["data"]
    assert data["reference"]["type"] == "order" and data["total"] == "₹950" and data["missing_details"]
    oid = data["order_id"]
    upd = await agent.run_tool(
        ToolCall(
            id="3",
            name="order_update",
            args={
                "order_id": oid,
                "details": {
                    "eggless": "no",
                    "delivery_date": future(),
                    "delivery_slot": "10am-1pm",
                    "address": "Warje",
                },
            },
        ),
        ctx.__class__(**{**ctx.__dict__, "step": 3}),
        bound,
    )
    assert upd["data"]["total"] == "₹1,000" and upd["data"]["missing_details"] == []
    conf = await agent.run_tool(
        ToolCall(id="4", name="order_confirm", args={"order_id": oid, "payment_method": "razorpay_link"}),
        ctx.__class__(**{**ctx.__dict__, "step": 4}),
        bound,
    )
    link_id = conf["data"]["reference"]["id"]
    assert conf["data"]["status"] == "awaiting_payment"

    from reflexo.agent.grounding import hydrate
    from reflexo.agent.output_gate import check
    from reflexo.agent.schemas import AgentOutput

    out = AgentOutput.model_validate(
        {
            "action": "send",
            "topic": "order_status",
            "language": "en",
            "blocks": [
                {"type": "text", "text": "Your total is ₹1,000 including ₹50 delivery."},
                {"type": "payment_link", "payment_link_id": link_id},
            ],
            "referenced_records": [{"type": "order", "id": oid}],
        }
    )
    assert check(out, await hydrate(out, shop["bid"], customer_id=shop["cust"], customer_phone=None)).passed
    wrong = out.model_copy(update={"blocks": [{"type": "text", "text": "Total ₹900 only"}]})
    wrong = AgentOutput.model_validate(wrong.model_dump())
    assert "amounts" in [
        c.name
        for c in check(wrong, await hydrate(wrong, shop["bid"], customer_id=shop["cust"], customer_phone=None)).failures
    ]
    foreign = AgentOutput.model_validate(
        {**out.model_dump(mode="json"), "blocks": [{"type": "payment_link", "payment_link_id": str(new_id())}]}
    )
    assert "references" in [
        c.name
        for c in check(
            foreign, await hydrate(foreign, shop["bid"], customer_id=shop["cust"], customer_phone=None)
        ).failures
    ]

    views = registry.require(__import__("reflexo.common.contracts", fromlist=["CommerceViewsPort"]).CommerceViewsPort)  # type: ignore[type-abstract]
    pl = await views.payment_link(shop["bid"], UUID(link_id))
    assert (pl.url, pl.amount_display) == (rzp.created[0]["url"], "₹1,000")
    summary = await views.order_summary(shop["bid"], UUID(oid))
    assert summary.total_display == "₹1,000" and summary.payment == "Payment pending"


async def test_order_start_takes_details_in_one_call(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    """Live regression: "1 kg truffle, eggless, Kothrud, day after tomorrow evening" in one message."""
    shop = await shop_with_customer(db)
    truffle = shop["cards"]["Chocolate Truffle Cake"]
    ctx = agent.ToolContext(
        business_id=shop["bid"],
        desk="customer",
        channel="whatsapp",
        conversation_id=shop["conv"],
        customer_id=shop["cust"],
        member_id=None,
        turn_id=new_id(),
        step=1,
    )
    args = {
        "items": [{"offering_id": str(truffle.offering_id), "variant_id": str(variant(truffle, "1 kg"))}],
        "details": {
            "eggless": "yes",
            "address": "Kothrud 411038",
            "delivery_slot": "evening",
            "delivery_date": "2023-10-13",
        },
    }
    res = await agent.run_tool(ToolCall(id="1", name="order_start", args=args), ctx, {"order_start"})
    data = res["data"]
    assert data["details"] == {"eggless": "yes", "address": "Kothrud 411038", "delivery_slot": "4pm-8pm"}
    assert [e["key"] for e in data["not_accepted"]] == ["delivery_date"] and "not accepted" in data["next"]
    assert data["total"] == "₹1,050" and [m["key"] for m in data["missing_details"]] == ["delivery_date"]


async def test_payment_events_without_link_reference_are_processed_quietly(
    registry: Registry, db: Database, rzp: FakeRazorpay
) -> None:
    """Live: payment.failed / payment.captured carry no link reference; their jobs died on a logging TypeError."""
    shop = await shop_with_customer(db)
    order = await ready_order(shop)
    await commerce.confirm_order(shop["bid"], order.order_id, "razorpay_link", idem="q1")
    before = len(await q(db, "SELECT id FROM outbound_messages WHERE conversation_id = :c", c=shop["conv"]))
    for kind in ("payment.failed", "payment.captured"):
        event = {"event": kind, "payload": {"payment": {"entity": {"id": f"pay_{rzp.tag}", "amount": 105000}}}}
        assert (await post_webhook(event, event_id=f"evt_{kind}_{rzp.tag}")).status_code == 200
    assert await run_jobs(registry, "commerce.payment_event") == 2  # handlers return; nothing raises or retries
    rows = await q(
        db,
        "SELECT processed_at FROM integration_events_log WHERE external_event_id LIKE :e",
        e=f"evt_%_{rzp.tag}",
    )
    assert len(rows) == 2 and all(r.processed_at is not None for r in rows)
    after = len(await q(db, "SELECT id FROM outbound_messages WHERE conversation_id = :c", c=shop["conv"]))
    assert after == before  # no "didn't go through" message mid-checkout
