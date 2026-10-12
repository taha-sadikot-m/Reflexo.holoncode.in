"""Confirm, payment links and payment events (POC_05 §4.1, §4.4–4.6).

* `confirm_order`: required details → automation guards (capacity) → COD rule → Razorpay link or COD.
* The `payment_links` row (unique idempotency key) is written before Razorpay is called; Razorpay's unique
  `reference_id` (= our row id) makes a retried create return the same link.
* Paid webhooks and the reconciliation job share one idempotent `apply_paid`.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
from reflexo.commerce import orders
from reflexo.commerce.models import (
    NOT_CANCELLABLE,
    CancelResult,
    CommerceError,
    ConfirmResult,
    OrderView,
    PaymentMethod,
)
from reflexo.common.blocks import OrderSummaryView, PaymentLinkView
from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    ConversationSenderPort,
    DomainEventWriter,
    JobEnqueuer,
    NewDomainEvent,
    OrderGuardPort,
    OwnerNotifyPort,
)
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.ports import Registry
from reflexo.common.xray import emit

log = get_logger(__name__)
RECONCILE_QUEUE = "commerce.reconcile_link"
_registry: Registry | None = None


def bind(registry: Registry) -> None:
    global _registry
    _registry = registry


def _port(port: Any) -> Any:
    return _registry.optional(port) if _registry else None


async def _event(s: Any, business_id: UUID, type_: str, order: OrderView, **payload: Any) -> None:
    writer = _port(DomainEventWriter)
    if writer is not None:
        await writer.append(
            s,
            NewDomainEvent(
                business_id=business_id,
                type=type_,
                source="commerce",
                correlation_id=order.order_id,
                customer_id=order.customer_id,
                conversation_id=order.conversation_id,
                payload={
                    "order_id": str(order.order_id),
                    "order_number": order.order_number,
                    "total_paise": order.total_paise,
                    "payment_method": order.payment_method,
                    **payload,
                },
            ),
        )


def _owner_text(v: OrderView, settings: dict[str, Any], headline: str) -> str:
    items = ", ".join(
        f"{ln.name}" + (f" ×{ln.quantity}" if ln.quantity > 1 else "") for ln in v.lines if not ln.adjustment
    )
    eggless = " (eggless)" if any(ln.adjustment == "eggless" for ln in v.lines) else ""
    details = {f.key: f.value for f in v.fields}
    when = " ".join(x for x in (details.get("delivery_date"), details.get("delivery_slot")) if x)
    msg = f" '{details['message_on_cake']}'" if details.get("message_on_cake") else ""
    where = details.get("address") or ""
    return (
        f"🧁 {headline} {v.order_number} — {items}{eggless}{msg}. {when} {where}. {format_inr(v.total_paise)}".strip()
    )


async def _notify_owner(business_id: UUID, kind: str, body: str, dedupe: str, about: UUID | None = None) -> None:
    notifier = _port(OwnerNotifyPort)
    if notifier is None:
        log.info("owner_notify_unavailable", kind=kind)
        return
    try:
        await notifier.notify(
            business_id, kind, body, dedupe_key=dedupe, priority=PriorityClass.P1, about_conversation=about
        )
    except Exception:  # a notification failure must never undo a payment
        log.exception("owner_notify_failed", kind=kind)


async def _send_customer(
    business_id: UUID, conversation_id: UUID | None, blocks: list[dict[str, Any]], key: str
) -> None:
    sender = _port(ConversationSenderPort)
    if sender is None or conversation_id is None:
        return
    try:
        await sender.send_blocks(business_id, conversation_id, blocks, origin="system", idempotency_base=key)
    except Exception:
        log.exception("customer_send_failed", key=key)


# ---------------------------------------------------------------- confirm


async def confirm_order(business_id: UUID, order_id: UUID, method: PaymentMethod, *, idem: str) -> ConfirmResult:
    settings = await knowledge.business_settings(business_id)
    async with get_db().tenant_session(business_id) as s:
        row = (await s.execute(text("SELECT status FROM orders WHERE id = :id FOR UPDATE"), {"id": order_id})).first()
        if row is None:
            raise CommerceError("NOT_FOUND", "order not found", guidance="Start a new order with order_start.")
        await orders.recompute(s, business_id, order_id) if str(row.status) == "draft" else None
        v = await orders.view(s, order_id)
        if v.status == "awaiting_payment":  # asked again: hand back the live link
            link = (
                await s.execute(
                    text("""SELECT id, short_url FROM payment_links WHERE order_id = :o AND status = 'created'
                              AND (expires_at IS NULL OR expires_at > now())
                            ORDER BY created_at DESC LIMIT 1"""),
                    {"o": order_id},
                )
            ).first()
            if link is not None:
                return ConfirmResult(
                    status="awaiting_payment", order=v, payment_link_id=link.id, payment_url=link.short_url
                )
        elif v.status != "draft":
            return ConfirmResult(
                status="confirmed" if v.status != "cancelled" else "declined",
                order=v,
                decline_code="ALREADY_" + v.status.upper() if v.status == "cancelled" else None,
            )
        if not v.lines:
            raise CommerceError(
                "VALIDATION_FAILED", "the order has no items", guidance="Ask what they'd like to order."
            )
        if v.missing_details:
            labels = ", ".join(f.label for f in v.fields if f.key in v.missing_details)
            raise CommerceError(
                "DETAILS_MISSING", f"missing: {labels}", guidance=f"Ask for: {labels} (one or two at a time)."
            )

        guard = _port(OrderGuardPort)
        if guard is not None:
            result = await guard.check(business_id, v.model_dump(mode="json"))
            if not result.allowed:
                return ConfirmResult(
                    status="declined",
                    order=v,
                    decline_code=result.code or "GUARD_DENIED",
                    decline_reason=result.reason,
                    alternatives=list(result.alternatives),
                )

        if method == "cod":
            rule = settings.get("cod_rule") or {}
            max_total = rule.get("max_total_paise")
            if not rule.get("enabled") or (max_total is not None and v.total_paise > int(max_total)):
                reason = (
                    f"COD only for orders up to {format_inr(int(max_total))}"
                    if rule.get("enabled") and max_total
                    else "Cash on delivery is not available"
                )
                if guard is not None:
                    await guard.release(business_id, order_id, "cod_declined")
                return ConfirmResult(
                    status="declined",
                    order=v,
                    decline_code="COD_NOT_ALLOWED",
                    decline_reason=reason,
                    alternatives=["razorpay_link"],
                )
            await s.execute(
                text("""UPDATE orders SET status = 'confirmed', payment_method = 'cod',
                                    payment_status = 'cod_pending', confirmed_at = now(), updated_at = now()
                                    WHERE id = :id"""),
                {"id": order_id},
            )
            v = await orders.view(s, order_id)
            await _event(s, business_id, "order.confirmed", v)
            confirmed = ConfirmResult(status="confirmed", order=v)
        else:
            confirmed = await _create_link(s, business_id, v, settings, idem)
    if confirmed.status == "confirmed":
        await _notify_owner(
            business_id,
            "order_cod",
            _owner_text(confirmed.order, settings, "New COD order"),
            f"order_confirmed:{order_id}",
            confirmed.order.conversation_id,
        )
    await emit(
        "order_confirm",
        PipelineEventStatus.OK,
        business_id=business_id,
        method=method,
        result=confirmed.status,
        order_number=confirmed.order.order_number,
        total_paise=confirmed.order.total_paise,
    )
    return confirmed


async def _create_link(s: Any, business_id: UUID, v: OrderView, settings: dict[str, Any], idem: str) -> ConfirmResult:
    cfg = get_settings()
    customer = (
        await s.execute(text("SELECT display_name, phone_e164 FROM customers WHERE id = :c"), {"c": v.customer_id})
    ).first()
    link_id = new_id()
    inserted = (
        await s.execute(
            text("""INSERT INTO payment_links (id, business_id, order_id, customer_id, amount_paise, idempotency_key,
                                               expires_at)
                    VALUES (:id, :b, :o, :c, :amt, :k, now() + make_interval(hours => :ttl))
                    ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"""),
            {
                "id": link_id,
                "b": business_id,
                "o": v.order_id,
                "c": v.customer_id,
                "amt": v.total_paise,
                "k": f"order:{v.order_id}:{idem}",
                "ttl": cfg.payment_link_ttl_h,
            },
        )
    ).scalar_one_or_none()
    if inserted is None:  # same confirm replayed: reuse its row
        link_id = (
            await s.execute(
                text("SELECT id FROM payment_links WHERE idempotency_key = :k"), {"k": f"order:{v.order_id}:{idem}"}
            )
        ).scalar_one()
    row = (
        await s.execute(
            text("SELECT provider_link_id, short_url, amount_paise FROM payment_links WHERE id = :id"), {"id": link_id}
        )
    ).one()
    if row.provider_link_id is None:
        link = await integrations.razorpay().create_payment_link(
            amount_paise=int(row.amount_paise),
            reference_id=str(link_id),
            description=f"{settings.get('name')} — Order {v.order_number}",
            expire_by=int(time.time()) + cfg.payment_link_ttl_h * 3600,
            customer_name=customer.display_name if customer else None,
            customer_contact=customer.phone_e164 if customer and customer.phone_e164 else None,
            notes={"order_id": str(v.order_id), "business_id": str(business_id), "order_number": v.order_number},
        )
        await s.execute(
            text("UPDATE payment_links SET provider_link_id = :p, short_url = :u WHERE id = :id"),
            {"p": link.id, "u": link.short_url, "id": link_id},
        )
        short_url = link.short_url
    else:
        short_url = row.short_url
    await s.execute(
        text("""UPDATE orders SET status = 'awaiting_payment', payment_method = 'razorpay_link',
                            payment_status = 'pending', updated_at = now() WHERE id = :id"""),
        {"id": v.order_id},
    )
    v = await orders.view(s, v.order_id)
    await _event(s, business_id, "payment.link_created", v, payment_link_id=str(link_id))
    enqueuer = _port(JobEnqueuer)
    if enqueuer is not None:
        await enqueuer.enqueue(
            RECONCILE_QUEUE,
            {"business_id": str(business_id), "payment_link_id": str(link_id), "attempt": 0},
            business_id=business_id,
            priority=PriorityClass.P2,
            dedupe_key=f"reconcile:{link_id}:0",
            delay_s=cfg.razorpay_reconcile_s,
            session=s,
        )
    return ConfirmResult(status="awaiting_payment", order=v, payment_link_id=link_id, payment_url=short_url)


# ---------------------------------------------------------------- payment events


async def _find_link(provider_link_id: str | None, reference_id: str | None) -> Any:
    async with get_db().service_session(ServiceReason.INTEGRATION_ROUTING) as s:
        return (
            await s.execute(
                text("""SELECT id, business_id, order_id, amount_paise, status FROM payment_links
                        WHERE (CAST(:p AS text) IS NOT NULL AND provider_link_id = :p)
                           OR (CAST(:r AS text) IS NOT NULL AND id::text = :r)
                        LIMIT 1"""),
                {"p": provider_link_id, "r": reference_id},
            )
        ).first()


async def apply_paid(
    business_id: UUID, link_id: UUID, *, payment_id: str | None, amount_paid: int | None, source: str
) -> bool:
    """Idempotent: link → paid, order → confirmed (+events), then customer summary + owner ping. False if already."""
    settings = await knowledge.business_settings(business_id)
    async with get_db().tenant_session(business_id) as s:
        link = (await s.execute(text("SELECT * FROM payment_links WHERE id = :id FOR UPDATE"), {"id": link_id})).one()
        if str(link.status) == "paid":
            return False
        if amount_paid is not None and amount_paid < int(link.amount_paise):
            log.warning("payment_amount_mismatch", link_id=str(link_id), expected=link.amount_paise, paid=amount_paid)
            return False
        await s.execute(
            text("""UPDATE payment_links SET status = 'paid', provider_payment_id = :pid, paid_at = now()
                                WHERE id = :id"""),
            {"pid": payment_id, "id": link_id},
        )
        await s.execute(
            text("""UPDATE orders SET status = 'confirmed', payment_status = 'paid',
                                confirmed_at = COALESCE(confirmed_at, now()), updated_at = now()
                                WHERE id = :o AND status IN ('awaiting_payment', 'draft')"""),
            {"o": link.order_id},
        )
        v = await orders.view(s, link.order_id)
        await _event(s, business_id, "payment.captured", v, payment_link_id=str(link_id), source=source)
        if v.status == "cancelled":  # money arrived for an order that no longer exists: never "confirmed"
            await _event(s, business_id, "payment.orphaned", v, payment_link_id=str(link_id), amount=link.amount_paise)
    if v.status == "cancelled":
        amount = format_inr(int(link.amount_paise))
        await _send_customer(
            business_id,
            v.conversation_id,
            [
                {
                    "type": "text",
                    "text": f"Payment of {amount} received, but order {v.order_number} was cancelled. "
                    "The shop will refund it — they'll be in touch 🙏",
                }
            ],
            f"paid:{link_id}",
        )
        await _notify_owner(
            business_id,
            "refund_due",
            f"💸 {amount} paid for cancelled order {v.order_number} — refund needed.",
            f"refund_due:{link_id}",
            v.conversation_id,
        )
        await emit("payment", PipelineEventStatus.OK, business_id=business_id, result="paid_after_cancel")
        return True
    async with get_db().tenant_session(business_id) as s:
        await _event(s, business_id, "order.confirmed", v)
    await _send_customer(
        business_id,
        v.conversation_id,
        [
            {"type": "text", "text": "Payment received ✅ Thank you! Your order is confirmed."},
            {"type": "order_summary", "order_id": str(v.order_id)},
        ],
        f"paid:{link_id}",
    )
    await _notify_owner(
        business_id,
        "order_confirmed",
        _owner_text(v, settings, "New order") + " paid ✅",
        f"order_confirmed:{v.order_id}",
        v.conversation_id,
    )
    await emit(
        "payment",
        PipelineEventStatus.OK,
        business_id=business_id,
        conversation_id=v.conversation_id,
        order_number=v.order_number,
        source=source,
        result="paid",
    )
    return True


async def apply_expired(business_id: UUID, link_id: UUID, status: str = "expired") -> None:
    async with get_db().tenant_session(business_id) as s:
        link = (
            await s.execute(
                text("SELECT order_id, status FROM payment_links WHERE id = :id FOR UPDATE"), {"id": link_id}
            )
        ).one()
        if str(link.status) != "created":
            return
        await s.execute(
            text("UPDATE payment_links SET status = CAST(:st AS payment_link_status) WHERE id = :id"),
            {"st": status, "id": link_id},
        )
        await s.execute(
            text("""UPDATE orders SET status = 'draft', payment_status = 'unpaid', updated_at = now()
                                WHERE id = :o AND status = 'awaiting_payment'"""),
            {"o": link.order_id},
        )
        v = await orders.view(s, link.order_id)
        await _event(s, business_id, "payment.link_expired", v, payment_link_id=str(link_id))
    guard = _port(OrderGuardPort)
    if guard is not None:
        await guard.release(business_id, v.order_id, "payment_link_expired")


async def cancel_order(
    business_id: UUID, order_id: UUID, *, by: str, reason: str | None = None, notify_owner: bool = True
) -> CancelResult:
    """The `order.cancel` capability's effect (POC_10 §3). Only invariants are checked here — whether the business
    allows it is the playbook's decision, made before this is called. Generic side effects, in one place:
    a live payment link is cancelled, capacity reserved by guards is released, `order.cancelled` is emitted (the
    automation router ends every run for this order), the owner is told unless they did it themselves."""
    link_to_cancel: str | None = None
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(text("SELECT status, payment_status FROM orders WHERE id = :o FOR UPDATE"), {"o": order_id})
        ).first()
        if row is None:
            raise CommerceError("NOT_FOUND", "order not found")
        status = str(row.status)
        if status == "cancelled":
            return CancelResult(status="already_cancelled", order=await orders.view(s, order_id))
        if status in NOT_CANCELLABLE:
            raise CommerceError("NOT_CANCELLABLE", f"cannot cancel: {NOT_CANCELLABLE[status]}")
        live = (
            await s.execute(
                text("""SELECT id, provider_link_id FROM payment_links WHERE order_id = :o AND status = 'created'
                        ORDER BY created_at DESC"""),
                {"o": order_id},
            )
        ).all()
        for link in live:
            await s.execute(
                text("UPDATE payment_links SET status = 'cancelled' WHERE id = :id AND status = 'created'"),
                {"id": link.id},
            )
            link_to_cancel = link_to_cancel or link.provider_link_id
        paid = (
            await s.execute(
                text("""SELECT COALESCE(sum(amount_paise), 0) FROM payment_links
                        WHERE order_id = :o AND status = 'paid'"""),
                {"o": order_id},
            )
        ).scalar_one()
        await s.execute(
            text("""UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = :by,
                                      cancel_reason = :why, updated_at = now() WHERE id = :o"""),
            {"by": by, "why": (reason or "")[:500] or None, "o": order_id},
        )
        v = await orders.view(s, order_id)
        await _event(s, business_id, "order.cancelled", v, by=by, reason=reason, refund_due_paise=int(paid))
    if link_to_cancel and integrations.razorpay().configured:
        try:  # the local row is already cancelled; a late payment on it is caught by apply_paid
            await integrations.razorpay().cancel_payment_link(link_to_cancel)
        except Exception:
            log.warning("payment_link_cancel_failed", order_id=str(order_id), exc_info=True)
    guard = _port(OrderGuardPort)
    if guard is not None:
        await guard.release(business_id, order_id, "order_cancelled")
    if notify_owner and by != "owner":  # the agent sends its own FYI citing the rule it acted on
        refund = f" Paid {format_inr(int(paid))} — refund needed." if paid else ""
        who = "the customer" if by == "customer" else by
        await _notify_owner(
            business_id,
            "order_cancelled",
            f"❌ Order {v.order_number} cancelled by {who}" + (f": “{reason}”" if reason else ".") + refund,
            f"order_cancelled:{order_id}",
            v.conversation_id,
        )
    await emit("order_cancel", PipelineEventStatus.OK, business_id=business_id, by=by, refund_due=int(paid))
    return CancelResult(
        status="cancelled", order=v, payment_link_cancelled=bool(link_to_cancel), refund_due_paise=int(paid)
    )


async def change_order(
    business_id: UUID, order_id: UUID, details: dict[str, Any], *, by: str, notify_owner: bool = True
) -> OrderView:
    """The `order.change` capability's effect (POC_10 §3): change details (date, slot, address, …) of a placed
    order. Only invariants here — whether the business allows it is the playbook's decision. A changed date moves
    the order's capacity reservation (guards re-checked; the old one is restored if the new day is full); a change
    that alters the total of an order already paid or awaiting payment is refused (the payment would not match)."""
    guard = _port(OrderGuardPort)
    async with get_db().tenant_session(business_id) as s:
        before = await orders.view(s, order_id)
        if before.status == "cancelled" or before.status in NOT_CANCELLABLE:
            reason = NOT_CANCELLABLE.get(before.status, "it was cancelled")
            raise CommerceError("NOT_CHANGEABLE", f"cannot change: {reason}")
        merged = await orders.validated_changes(s, business_id, order_id, details)
    after_preview = before.model_copy(
        update={k: merged.get(k) for k in ("delivery_date", "delivery_slot", "address") if k in merged}
    )
    moved = before.status != "draft" and str(after_preview.delivery_date) != str(before.delivery_date)
    if moved and guard is not None:
        await guard.release(business_id, order_id, "order_changed")
        result = await guard.check(business_id, after_preview.model_dump(mode="json"))
        if not result.allowed:
            await guard.check(business_id, before.model_dump(mode="json"))  # keep the old day's reservation
            raise CommerceError(
                result.code or "GUARD_DENIED",
                result.reason or "that day is full",
                guidance="Offer another day" + (f": {', '.join(result.alternatives)}" if result.alternatives else "."),
            )
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("UPDATE orders SET details = CAST(:d AS jsonb), updated_at = now() WHERE id = :id"),
            {"d": json.dumps(merged, ensure_ascii=False, default=str), "id": order_id},
        )
        await orders.recompute(s, business_id, order_id)
        after = await orders.view(s, order_id)
        if after.total_paise != before.total_paise and before.payment_status in ("paid", "pending"):
            raise CommerceError(  # rolls the transaction back
                "NOT_CHANGEABLE",
                f"the total would become {format_inr(after.total_paise)} (paid: {format_inr(before.total_paise)})",
                guidance="The payment would not match; the owner has to handle this.",
            )
        old = {f.key: f.value for f in before.fields}
        changed = {k: v for k, v in merged.items() if old.get(k) != v}
        await _event(s, business_id, "order.changed", after, by=by, changes=changed)
    if notify_owner and by != "owner":
        await _notify_owner(
            business_id,
            "order_changed",
            f"✏️ Order {after.order_number} changed by the customer: "
            + ", ".join(f"{k.replace('_', ' ')} → {v}" for k, v in changed.items()),
            f"order_changed:{order_id}:{new_id().hex[:8]}",  # every change is news
            after.conversation_id,
        )
    await emit("order_change", PipelineEventStatus.OK, business_id=business_id, by=by, moved=moved)
    return after


async def refund_order(business_id: UUID, order_id: UUID, *, by: str, reason: str | None = None) -> dict[str, Any]:
    """The `refund.request` capability's effect (POC_10 §3): refund the order's captured online payment in full.
    Invariants only — whether to refund is the owner's decision (money: proposed by default). Without a provider
    payment (or Razorpay not configured) the refund is recorded and the owner is told to do it by hand."""
    async with get_db().tenant_session(business_id) as s:
        o = (
            await s.execute(
                text("SELECT order_number, payment_status FROM orders WHERE id = :o FOR UPDATE"), {"o": order_id}
            )
        ).first()
        if o is None:
            raise CommerceError("NOT_FOUND", "order not found")
        if str(o.payment_status) == "refunded":
            raise CommerceError("ALREADY_REFUNDED", "this order was already refunded")
        link = (
            await s.execute(
                text("""SELECT id, amount_paise, provider_payment_id FROM payment_links
                        WHERE order_id = :o AND status = 'paid' ORDER BY paid_at DESC LIMIT 1"""),
                {"o": order_id},
            )
        ).first()
        if str(o.payment_status) != "paid" or link is None:
            raise CommerceError("NOTHING_TO_REFUND", "nothing was paid online for this order")
    refund_id: str | None = None
    if link.provider_payment_id and integrations.razorpay().configured:
        try:
            refund = await integrations.razorpay().refund_payment(
                link.provider_payment_id, int(link.amount_paise), {"order": o.order_number, "by": by}
            )
            refund_id = str(refund.get("id"))
        except Exception as exc:
            log.warning("refund_failed", order=o.order_number, error=str(exc)[:200])
            raise CommerceError("REFUND_FAILED", "the payment provider refused the refund") from exc
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("UPDATE orders SET payment_status = 'refunded', updated_at = now() WHERE id = :o"), {"o": order_id}
        )
        v = await orders.view(s, order_id)
        await _event(s, business_id, "payment.refunded", v, by=by, reason=reason, refund_id=refund_id,
                     amount_paise=int(link.amount_paise))  # fmt: skip
    amount = format_inr(int(link.amount_paise))
    how = f"refund {refund_id} started" if refund_id else "please refund it from the Razorpay dashboard"
    await _notify_owner(
        business_id, "refund", f"💸 {amount} for {o.order_number}: {how}.", f"refund:{order_id}", v.conversation_id
    )
    return {"refunded": amount, "order_number": o.order_number, "provider_refund": bool(refund_id)}


async def handle_payment_event(log_id: UUID) -> None:
    """Job `commerce.payment_event`: one verified, deduplicated Razorpay webhook."""
    async with get_db().service_session(ServiceReason.INTEGRATION_ROUTING) as s:
        row = (
            await s.execute(text("SELECT type, payload FROM integration_events_log WHERE id = :id"), {"id": log_id})
        ).one()
    payload = row.payload.get("payload") or {}
    pl = (payload.get("payment_link") or {}).get("entity") or {}
    pay = (payload.get("payment") or {}).get("entity") or {}
    ref = pl.get("reference_id") or (pay.get("notes") or {}).get("payment_link_ref")
    link = await _find_link(pl.get("id"), ref)
    if link is None:  # e.g. payment.* events without a link reference: payment_link.* carries the outcome
        log.info("payment_event_unmatched", event_type=row.type, link=pl.get("id"))
        async with get_db().service_session(ServiceReason.INTEGRATION_ROUTING) as s:
            await s.execute(
                text("UPDATE integration_events_log SET processed_at = now() WHERE id = :id"), {"id": log_id}
            )
        return
    if row.type == "payment_link.paid":
        await apply_paid(
            link.business_id,
            link.id,
            payment_id=pay.get("id"),
            amount_paid=int(pl.get("amount_paid") or pay.get("amount") or 0) or None,
            source="webhook",
        )
    elif row.type in ("payment_link.expired", "payment_link.cancelled"):
        await apply_expired(link.business_id, link.id, "expired" if row.type.endswith("expired") else "cancelled")
    # payment.failed: recorded only. Razorpay's checkout already shows the failure and the customer retries on the
    # same page (live: failed at 10:06:30, paid 10 s later) — a WhatsApp "didn't go through" mid-retry is noise.
    with_time = row.payload.get("created_at")
    await emit("payment", PipelineEventStatus.OK, business_id=link.business_id, event=row.type, created_at=with_time)
    async with get_db().service_session(ServiceReason.INTEGRATION_ROUTING) as s:
        await s.execute(
            text("UPDATE integration_events_log SET processed_at = now(), business_id = :b WHERE id = :id"),
            {"b": link.business_id, "id": log_id},
        )


async def reconcile_link(business_id: UUID, link_id: UUID, attempt: int) -> None:
    """Job `commerce.reconcile_link`: catches payments whose webhook never arrived; re-schedules with backoff."""
    async with get_db().tenant_session(business_id) as s:
        link = (
            await s.execute(
                text("SELECT status, provider_link_id, expires_at FROM payment_links WHERE id = :id"), {"id": link_id}
            )
        ).first()
    if link is None or str(link.status) != "created" or not link.provider_link_id:
        return
    remote = await integrations.razorpay().fetch_payment_link(link.provider_link_id)
    if remote.status == "paid":
        await apply_paid(
            business_id, link_id, payment_id=remote.payment_id, amount_paid=remote.amount, source="reconcile"
        )
        return
    if remote.status in ("expired", "cancelled"):
        await apply_expired(business_id, link_id, remote.status)
        return
    enqueuer = _port(JobEnqueuer)
    delay = min(get_settings().razorpay_reconcile_s * (2 ** min(attempt + 1, 4)), 900)
    if enqueuer is not None and attempt < 60:
        await enqueuer.enqueue(
            RECONCILE_QUEUE,
            {"business_id": str(business_id), "payment_link_id": str(link_id), "attempt": attempt + 1},
            business_id=business_id,
            priority=PriorityClass.P2,
            dedupe_key=f"reconcile:{link_id}:{attempt + 1}",
            delay_s=delay,
        )


# ---------------------------------------------------------------- block views (CommerceViewsPort)


class CommerceViews:
    async def payment_link(self, business_id: UUID, payment_link_id: UUID) -> PaymentLinkView:
        async with get_db().tenant_session(business_id) as s:
            r = (
                await s.execute(
                    text("""SELECT pl.short_url, pl.amount_paise, pl.status, o.order_number
                            FROM payment_links pl JOIN orders o ON o.id = pl.order_id WHERE pl.id = :id"""),
                    {"id": payment_link_id},
                )
            ).first()
        if r is None or not r.short_url or str(r.status) != "created":
            raise LookupError("payment link not available")
        return PaymentLinkView(
            payment_link_id=payment_link_id,
            url=r.short_url,
            amount_display=format_inr(r.amount_paise),
            description=f"Order {r.order_number}",
        )

    async def order_summary(self, business_id: UUID, order_id: UUID) -> OrderSummaryView:
        async with get_db().tenant_session(business_id) as s:
            v = await orders.view(s, order_id)
        details = {f.key: f.value for f in v.fields}
        delivery = (
            " ".join(
                str(x)
                for x in (details.get("delivery_date"), details.get("delivery_slot"), details.get("address"))
                if x
            )
            or None
        )
        payment = {"paid": "Paid ✅", "cod_pending": "Cash on delivery", "pending": "Payment pending"}.get(
            v.payment_status
        )
        return OrderSummaryView(
            order_id=order_id,
            order_number=v.order_number,
            lines=orders.summary_lines(v),
            total_display=format_inr(v.total_paise),
            delivery=delivery,
            payment=payment,
        )


async def grounding_amounts(business_id: UUID, order_ids: list[UUID], link_ids: list[UUID]) -> dict[str, Any]:
    """Amounts and URLs the output gate may accept for referenced orders / payment links (re-read under RLS)."""
    out: dict[str, Any] = {"orders": set(), "links": set(), "amounts": set(), "urls": set()}
    async with get_db().tenant_session(business_id) as s:
        for r in (
            (
                await s.execute(
                    text("SELECT id, subtotal_paise, delivery_fee_paise, total_paise FROM orders WHERE id = ANY(:ids)"),
                    {"ids": order_ids},
                )
            ).all()
            if order_ids
            else []
        ):
            out["orders"].add(str(r.id))
            out["amounts"] |= {int(r.subtotal_paise), int(r.delivery_fee_paise), int(r.total_paise)}
            for ln in (
                await s.execute(
                    text("SELECT line_total_paise, unit_price_paise FROM order_items WHERE order_id = :o"), {"o": r.id}
                )
            ).all():
                out["amounts"] |= {int(ln.line_total_paise), int(ln.unit_price_paise)}
        for r in (
            (
                await s.execute(
                    text("SELECT id, amount_paise, short_url FROM payment_links WHERE id = ANY(:ids)"),
                    {"ids": link_ids},
                )
            ).all()
            if link_ids
            else []
        ):
            out["links"].add(str(r.id))
            out["amounts"].add(int(r.amount_paise))
            if r.short_url:
                out["urls"].add(r.short_url)
    return out
