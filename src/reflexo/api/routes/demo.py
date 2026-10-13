"""Public demo API (`/api/demo`): open a private demo shop and sign the visitor in as its owner.

Everything after that goes through the owner-scoped admin API (`/admin/api`, owner cookie), exactly as a real
owner's app does — the demo is the product, not a mock-up of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

import reflexo.admin.api as admin
from reflexo.api.routes.admin import problem
from reflexo.common.config import get_settings

router = APIRouter(prefix="/api/demo")


@router.post("/start")
async def start(request: Request, response: Response) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    try:
        shop = await admin.demo.create(client)
    except admin.demo.DemoUnavailable as exc:
        if exc.retry_after_s:
            raise problem(429, "DEMO_LIMIT", exc.reason, {"Retry-After": str(exc.retry_after_s)}) from None
        raise problem(503, "DEMO_UNAVAILABLE", exc.reason) from None
    settings = get_settings()
    token, s = admin.issue_session("Meera (demo)", business_id=str(shop.business_id), hours=settings.demo_sandbox_hours)
    response.set_cookie(
        admin.OWNER_COOKIE,
        token,
        max_age=settings.demo_sandbox_hours * 3600,
        httponly=True,
        secure=settings.env != "dev",
        samesite="strict",
        path="/admin",
    )
    return {"business_id": str(shop.business_id), "name": shop.name, "csrf": s.csrf, "expires_at": s.expires_at}
