"""Public interface of the `commerce` module. Other modules import only from here (POC_01 §4).

Orders, order items, payment links, COD rules, order events. Spec: POC_05 §4.
"""

from __future__ import annotations

from uuid import UUID

from reflexo.commerce import payments
from reflexo.commerce.models import (
    CUSTOMER_ENTERED,
    NOT_CANCELLABLE,
    CancelResult,
    CommerceError,
    ConfirmResult,
    ItemRef,
    OrderView,
    PaymentMethod,
)
from reflexo.commerce.orders import get_order, open_order, order_status, start_order, update_order
from reflexo.commerce.payments import CommerceViews, apply_paid, cancel_order, confirm_order, grounding_amounts
from reflexo.common.contracts import CommerceViewsPort, JobHandlerRegistry, JobRow
from reflexo.common.ports import Registry

__all__ = [
    "CUSTOMER_ENTERED",
    "NOT_CANCELLABLE",
    "CancelResult",
    "CommerceError",
    "ConfirmResult",
    "ItemRef",
    "OrderView",
    "PaymentMethod",
    "apply_paid",
    "cancel_order",
    "confirm_order",
    "get_order",
    "grounding_amounts",
    "open_order",
    "order_status",
    "register",
    "start_order",
    "update_order",
]


def register(registry: Registry) -> None:
    payments.bind(registry)
    registry.provide(CommerceViewsPort, CommerceViews())  # type: ignore[type-abstract]
    jobs = registry.optional(JobHandlerRegistry)  # type: ignore[type-abstract]
    if jobs is None:
        return

    async def payment_event(job: JobRow) -> None:
        await payments.handle_payment_event(UUID(job.payload["log_id"]))

    async def reconcile(job: JobRow) -> None:
        p = job.payload
        await payments.reconcile_link(UUID(p["business_id"]), UUID(p["payment_link_id"]), int(p.get("attempt", 0)))

    jobs.register("commerce.payment_event", payment_event)
    jobs.register(payments.RECONCILE_QUEUE, reconcile)
