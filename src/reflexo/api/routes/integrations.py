"""Integration webhooks (POC_05 §4.5): thin; verification, dedupe and hand-off live in `integrations.api`."""

from __future__ import annotations

from html import escape
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

import reflexo.commerce.api as commerce
import reflexo.integrations.api as integrations

router = APIRouter()


@router.get("/integrations/razorpay/simulated/{payment_link_id}", include_in_schema=False)
async def simulated_payment_link(payment_link_id: UUID) -> HTMLResponse:
    """Where a simulated payment link opens: shows the order and amount. Read-only; the simulator's Pay button is
    what pays it (a GET never changes anything)."""
    link = await commerce.simulated_link(payment_link_id)
    if link is None:
        return HTMLResponse("<p>Payment link not found.</p>", status_code=404)
    body = (
        f"<h1>Test payment: order {escape(link['order_number'])}</h1>"
        f"<p>Amount: {escape(link['amount'])}. Status: {escape(link['status'].replace('_', ' '))}.</p>"
        "<p>This is a simulated payment link. No money moves. "
        "To pay it, use the Pay button under the chat in the simulator.</p>"
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width, initial-scale=1'><title>Test payment</title></head>"
        f"<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem'>{body}"
        "</body></html>"
    )


@router.post("/integrations/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request) -> Response:
    result = await integrations.ingest_razorpay(dict(request.headers), await request.body())
    return Response(content=result.body, status_code=result.status_code, media_type="text/plain")
