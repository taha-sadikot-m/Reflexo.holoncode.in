"""Integration webhooks (POC_05 §4.5): thin; verification, dedupe and hand-off live in `integrations.api`."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

import reflexo.integrations.api as integrations

router = APIRouter()


@router.post("/integrations/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request) -> Response:
    result = await integrations.ingest_razorpay(dict(request.headers), await request.body())
    return Response(content=result.body, status_code=result.status_code, media_type="text/plain")
