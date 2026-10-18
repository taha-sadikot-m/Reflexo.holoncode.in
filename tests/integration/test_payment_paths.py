"""The payment paths' invariants (safety core, BETA_08 §2.4; CHK-Q-2 needs them covered): what cannot be confirmed,
late, short or orphaned payments, expiry, cancellation and refunds when the provider misbehaves, and payments that
keep working when notifications fail."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.commerce.api as commerce
import reflexo.integrations.api as integrations
from reflexo.commerce import payments
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry
from tests.integration import test_commerce as tc
from tests.integration.test_commerce import (
    FakeRazorpay,
    Guard,
    future,
    paid_event,
    post_webhook,
    q,
    ready_order,
    run_jobs,
)

pytestmark = pytest.mark.integration
rzp = tc.rzp
guard = tc.guard
registry = tc.registry


async def shop(db: Database) -> dict[str, Any]:
    return await tc.shop_with_customer(db)


async def awaiting(db: Database, s: dict[str, Any], rzp: FakeRazorpay, idem: str) -> tuple[Any, Any]:
    order = await ready_order(s)
    res = await commerce.confirm_order(s["bid"], order.order_id, "razorpay_link", idem=idem)
    assert res.status == "awaiting_payment"
    return order, res


async def link_status(db: Database, link_id: UUID) -> str:
    [row] = await q(db, "SELECT status FROM payment_links WHERE id = :l", l=link_id)
    return str(row.status)


async def order_status(db: Database, order_id: UUID) -> tuple[str, str]:
    [row] = await q(db, "SELECT status, payment_status FROM orders WHERE id = :o", o=order_id)
    return str(row.status), str(row.payment_status)


# ---------------------------------------------------------------- confirm


async def test_confirm_refuses_what_cannot_be_confirmed(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    s = await shop(db)
    with pytest.raises(commerce.CommerceError) as nothing:
        await commerce.confirm_order(s["bid"], new_id(), "razorpay_link", idem="x0")
    assert nothing.value.code == "NOT_FOUND"

    card = s["cards"]["Chocolate Truffle Cake"]
    bare = await commerce.start_order(
        s["bid"],
        s["cust"],
        s["conv"],
        [commerce.ItemRef(offering_id=card.offering_id, variant_id=tc.variant(card, "1 kg"))],
    )
    with pytest.raises(commerce.CommerceError) as missing:
        await commerce.confirm_order(s["bid"], bare.order_id, "razorpay_link", idem="x1")
    assert missing.value.code == "DETAILS_MISSING" and "Delivery date" in str(missing.value)

    async with db.service_session(ServiceReason.ADMIN) as session:
        await session.execute(text("DELETE FROM order_items WHERE order_id = :o"), {"o": bare.order_id})
    with pytest.raises(commerce.CommerceError) as empty:
        await commerce.confirm_order(s["bid"], bare.order_id, "razorpay_link", idem="x2")
    assert empty.value.code == "VALIDATION_FAILED"

    order, res = await awaiting(db, s, rzp, "x3")  # the same open draft, now complete
    again = await commerce.confirm_order(s["bid"], order.order_id, "razorpay_link", idem="x4")
    assert again.payment_link_id == res.payment_link_id  # asked again: the live link, not a second one
    await commerce.cancel_order(s["bid"], order.order_id, by="customer", notify_owner=False)
    cancelled = await commerce.confirm_order(s["bid"], order.order_id, "razorpay_link", idem="x5")
    assert (cancelled.status, cancelled.decline_code) == ("declined", "ALREADY_CANCELLED")


async def test_a_confirmed_order_stays_confirmed(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "c1")
    assert await commerce.apply_paid(s["bid"], res.payment_link_id, payment_id="pay_1", amount_paid=None, source="t")
    again = await commerce.confirm_order(s["bid"], order.order_id, "razorpay_link", idem="c2")
    assert again.status == "confirmed" and again.decline_code is None and len(rzp.created) == 1


# ---------------------------------------------------------------- payments arriving


async def test_a_short_payment_never_confirms(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "p1")
    paid = await commerce.apply_paid(s["bid"], res.payment_link_id, payment_id="pay_x", amount_paid=100, source="t")
    assert paid is False and await order_status(db, order.order_id) == ("awaiting_payment", "pending")
    assert await link_status(db, res.payment_link_id) == "created"


async def test_webhooks_for_expiry_failure_and_unknown_links(
    registry: Registry, db: Database, rzp: FakeRazorpay
) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "w1")
    failed = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_f", "notes": {}}}}}
    unknown = paid_event("plink_nobody", str(new_id()), 100)
    expired = {
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"id": rzp.created[0]["id"], "reference_id": str(res.payment_link_id)}}},
    }
    for body, key in ((failed, "f"), (unknown, "u"), (expired, "e")):
        assert (await post_webhook(body, event_id=f"evt_{rzp.tag}_{key}")).status_code == 200
    assert await run_jobs(registry, "commerce.payment_event") == 3
    assert await link_status(db, res.payment_link_id) == "expired"
    assert await order_status(db, order.order_id) == ("draft", "unpaid")  # back to a draft the customer can finish
    await payments.apply_expired(s["bid"], res.payment_link_id, "cancelled")  # an expired link stays expired
    assert await link_status(db, res.payment_link_id) == "expired"


async def test_reconcile_finishes_with_the_provider_or_stops(
    registry: Registry, db: Database, rzp: FakeRazorpay
) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "r1")
    rzp.remote_status = "expired"  # Razorpay expired it and the webhook never came
    await payments.reconcile_link(s["bid"], res.payment_link_id, 0)
    assert await link_status(db, res.payment_link_id) == "expired"
    await payments.reconcile_link(s["bid"], res.payment_link_id, 1)  # nothing left to reconcile
    await payments.reconcile_link(s["bid"], new_id(), 0)  # an unknown link is ignored
    assert await order_status(db, order.order_id) == ("draft", "unpaid")


# ---------------------------------------------------------------- cancel and change


async def test_cancel_survives_a_provider_error(
    registry: Registry, db: Database, rzp: FakeRazorpay, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await shop(db)
    with pytest.raises(commerce.CommerceError, match="not found"):
        await commerce.cancel_order(s["bid"], new_id(), by="customer")
    order, res = await awaiting(db, s, rzp, "k1")

    async def broken(link_id: str) -> Any:
        raise integrations.RazorpayError("provider down", code="PROVIDER_UNAVAILABLE", retryable=True)

    monkeypatch.setattr(rzp, "cancel_payment_link", broken)
    result = await commerce.cancel_order(s["bid"], order.order_id, by="customer", reason="plans changed")
    assert result.status == "cancelled" and result.payment_link_cancelled  # our record is cancelled either way
    assert await link_status(db, res.payment_link_id) == "cancelled"
    with pytest.raises(commerce.CommerceError) as changed:
        await commerce.change_order(s["bid"], order.order_id, {"delivery_slot": "1pm-4pm"}, by="customer")
    assert changed.value.code == "NOT_CHANGEABLE"


async def test_a_full_day_keeps_the_old_date(registry: Registry, db: Database, rzp: FakeRazorpay, guard: Guard) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "g1")
    assert await commerce.apply_paid(s["bid"], res.payment_link_id, payment_id="pay_g", amount_paid=None, source="t")
    guard.deny = True
    with pytest.raises(commerce.CommerceError) as full:
        await commerce.change_order(s["bid"], order.order_id, {"delivery_date": future(5)}, by="customer")
    assert full.value.code == "CAPACITY_FULL" and "2026-10-16" in (full.value.guidance or "")
    [row] = await q(db, "SELECT details FROM orders WHERE id = :o", o=order.order_id)
    assert row.details["delivery_date"] == future()  # unchanged
    assert guard.released[-1] == "order_changed"


# ---------------------------------------------------------------- refunds


async def test_refund_outcomes(
    registry: Registry, db: Database, rzp: FakeRazorpay, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await shop(db)
    with pytest.raises(commerce.CommerceError, match="not found"):
        await commerce.refund_order(s["bid"], new_id(), by="owner")
    order, res = await awaiting(db, s, rzp, "f1")
    with pytest.raises(commerce.CommerceError) as unpaid:
        await commerce.refund_order(s["bid"], order.order_id, by="owner")
    assert unpaid.value.code == "NOTHING_TO_REFUND"

    assert await commerce.apply_paid(s["bid"], res.payment_link_id, payment_id="pay_r", amount_paid=None, source="t")

    async def refused(payment_id: str, amount_paise: int, notes: dict[str, str]) -> dict[str, Any]:
        raise integrations.RazorpayError("refund window closed", code="BAD_REQUEST_ERROR")

    monkeypatch.setattr(rzp, "refund_payment", refused, raising=False)
    with pytest.raises(commerce.CommerceError) as failed:
        await commerce.refund_order(s["bid"], order.order_id, by="owner")
    assert failed.value.code == "REFUND_FAILED" and await order_status(db, order.order_id) == ("confirmed", "paid")

    async with db.service_session(ServiceReason.ADMIN) as session:  # a payment the provider gave no id for
        await session.execute(text("UPDATE payment_links SET provider_payment_id = NULL WHERE id = :l"),
                              {"l": res.payment_link_id})  # fmt: skip
    manual = await commerce.refund_order(s["bid"], order.order_id, by="owner")
    assert manual["provider_refund"] is False  # recorded; the owner refunds from the dashboard
    with pytest.raises(commerce.CommerceError) as twice:
        await commerce.refund_order(s["bid"], order.order_id, by="owner")
    assert twice.value.code == "ALREADY_REFUNDED"


# ---------------------------------------------------------------- notifications never undo a payment


class Broken:
    async def notify(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("notifier down")

    async def send_blocks(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("sender down")


async def test_payment_survives_broken_notifications(
    registry: Registry, db: Database, rzp: FakeRazorpay, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflexo.common.contracts import ConversationSenderPort, OwnerNotifyPort

    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "n1")
    broken = Broken()
    original = payments._port
    monkeypatch.setattr(
        payments, "_port", lambda port: broken if port in (OwnerNotifyPort, ConversationSenderPort) else original(port)
    )
    assert await commerce.apply_paid(s["bid"], res.payment_link_id, payment_id="pay_n", amount_paid=None, source="t")
    assert await order_status(db, order.order_id) == ("confirmed", "paid")

    monkeypatch.setattr(payments, "_port", lambda port: None)  # no ports at all: no events, no notifications
    other = await shop(db)
    _, r2 = await awaiting(db, other, rzp, "n2")
    assert await commerce.apply_paid(other["bid"], r2.payment_link_id, payment_id="pay_m", amount_paid=None, source="t")
    with pytest.raises(LookupError):
        await payments.CommerceViews().payment_link(other["bid"], new_id())


# ---------------------------------------------------------------- more edges of the same paths


async def test_changes_drafts_and_cancellations(registry: Registry, db: Database, rzp: FakeRazorpay) -> None:
    s = await shop(db)
    order = await ready_order(s)
    moved = await commerce.change_order(s["bid"], order.order_id, {"delivery_date": future(5)}, by="customer")
    assert moved.delivery_date == future(5) and moved.total_paise == order.total_paise
    draft = await commerce.cancel_order(s["bid"], order.order_id, by="owner")  # no live link: nothing to cancel there
    assert draft.status == "cancelled" and not draft.payment_link_cancelled
    twice = await commerce.cancel_order(s["bid"], order.order_id, by="owner")
    assert twice.status == "already_cancelled"

    paid_order, res = await awaiting(db, await shop(db), rzp, "e1")
    bid = (await q(db, "SELECT business_id FROM orders WHERE id = :o", o=paid_order.order_id))[0].business_id
    assert await commerce.apply_paid(bid, res.payment_link_id, payment_id="pay_e", amount_paid=None, source="t")
    with pytest.raises(commerce.CommerceError) as total:  # a new total would not match what was paid
        await commerce.change_order(bid, paid_order.order_id, {"eggless": "no"}, by="customer")  # ₹50 less
    assert total.value.code == "NOT_CHANGEABLE"
    assert await order_status(db, paid_order.order_id) == ("confirmed", "paid")


async def test_a_link_past_its_time_is_replaced_and_reconcile_stops(
    registry: Registry, db: Database, rzp: FakeRazorpay
) -> None:
    s = await shop(db)
    order, res = await awaiting(db, s, rzp, "t1")
    async with db.service_session(ServiceReason.ADMIN) as session:
        await session.execute(text("UPDATE payment_links SET expires_at = now() - interval '1 minute' WHERE id = :l"),
                              {"l": res.payment_link_id})  # fmt: skip
    fresh = await commerce.confirm_order(s["bid"], order.order_id, "razorpay_link", idem="t2")
    assert fresh.status == "awaiting_payment" and fresh.payment_link_id != res.payment_link_id
    before = len(await q(db, "SELECT 1 FROM jobs WHERE queue = 'commerce.reconcile_link'"))
    await payments.reconcile_link(s["bid"], fresh.payment_link_id, 60)  # the last attempt schedules nothing more
    assert len(await q(db, "SELECT 1 FROM jobs WHERE queue = 'commerce.reconcile_link'")) == before

    failed = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_ff", "notes": {"payment_link_ref": str(fresh.payment_link_id)}}}},
    }
    assert (await post_webhook(failed, event_id=f"evt_{rzp.tag}_ff")).status_code == 200
    assert await run_jobs(registry, "commerce.payment_event") == 1
    assert await link_status(db, fresh.payment_link_id) == "created"  # a failed attempt is only recorded
