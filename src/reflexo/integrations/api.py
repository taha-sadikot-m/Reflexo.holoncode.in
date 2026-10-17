"""Public interface of the `integrations` module. Other modules import only from here (POC_01 §4).

Razorpay (payment links, webhook ingress), Shopify (F15). Spec: POC_05 §4.5, §5.

Webhooks are verified, deduplicated in `integration_events_log` and handed to commerce as a durable job
(`commerce.payment_event`), so the HTTP response never waits on business logic and a crash cannot lose a payment.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.xray import emit
from reflexo.integrations.razorpay import (
    SIMULATED_LINK_PREFIX,
    PaymentLink,
    RazorpayClient,
    RazorpayError,
    SimulatedRazorpay,
    sign,
    verify_signature,
)

__all__ = [
    "PAYMENT_EVENT_QUEUE",
    "SIMULATED_LINK_PREFIX",
    "PaymentLink",
    "RazorpayClient",
    "RazorpayError",
    "SimulatedRazorpay",
    "WebhookResult",
    "ingest_razorpay",
    "razorpay",
    "register",
    "sign",
    "verify_signature",
]

log = get_logger(__name__)
PAYMENT_EVENT_QUEUE = "commerce.payment_event"
HANDLED_EVENTS = frozenset(
    {"payment_link.paid", "payment_link.expired", "payment_link.cancelled", "payment.failed", "payment.captured"}
)
_registry: Registry | None = None
_client: RazorpayClient | None = None
_simulated = SimulatedRazorpay()


def register(registry: Registry) -> None:
    global _registry
    _registry = registry


def razorpay(*, simulated: bool = False) -> RazorpayClient | SimulatedRazorpay:
    """The payment-link provider for an order: simulated conversations never reach Razorpay."""
    global _client
    if simulated:
        return _simulated
    if _client is None:
        _client = RazorpayClient()
    return _client


@dataclass(frozen=True)
class WebhookResult:
    status_code: int
    body: str


async def ingest_razorpay(headers: Mapping[str, str], raw_body: bytes) -> WebhookResult:
    """verify → dedupe (event id) → log → enqueue job → 200. Unknown event types are logged and acknowledged."""
    lowered = {k.lower(): v for k, v in headers.items()}
    secret = get_settings().razorpay_webhook_secret.get_secret_value()
    if not verify_signature(raw_body, lowered.get("x-razorpay-signature"), secret):
        await emit("integration_webhook", PipelineEventStatus.ERROR, provider="razorpay", reason="signature")
        return WebhookResult(401, "invalid signature")
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return WebhookResult(400, "invalid json")
    event_type = str(payload.get("event") or "")
    event_id = lowered.get("x-razorpay-event-id") or "body:" + hashlib.sha256(raw_body).hexdigest()
    async with get_db().service_session(ServiceReason.INTEGRATION_ROUTING) as s:
        log_id = (
            await s.execute(
                text("""INSERT INTO integration_events_log (id, provider, external_event_id, type, payload)
                        VALUES (:id, 'razorpay', :e, :t, CAST(:p AS jsonb))
                        ON CONFLICT (provider, external_event_id) DO NOTHING RETURNING id"""),
                {"id": new_id(), "e": event_id, "t": event_type, "p": json.dumps(payload)},
            )
        ).scalar_one_or_none()
        if log_id is not None and event_type in HANDLED_EVENTS and _registry is not None:
            enqueuer = _registry.require(JobEnqueuer)  # type: ignore[type-abstract]
            await enqueuer.enqueue(
                PAYMENT_EVENT_QUEUE,
                {"log_id": str(log_id), "event": event_type},
                priority=PriorityClass.P0,
                dedupe_key=f"rzp:{event_id}",
                session=s,
            )
    await emit(
        "integration_webhook",
        PipelineEventStatus.OK if log_id else PipelineEventStatus.SKIP,
        provider="razorpay",
        event=event_type,
        duplicate=log_id is None,
    )
    return WebhookResult(200, "ok")
