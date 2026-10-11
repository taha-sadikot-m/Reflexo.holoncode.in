"""HTTP entrypoint (process `api`): webhooks, admin API, health (POC_01 §3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from sqlalchemy import text

from reflexo.bootstrap import bootstrap
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.redis import get_redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    bootstrap("reflexo-api")
    yield
    await get_db().dispose()
    await get_redis().close()


app = FastAPI(title="REFLEXO", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        async with get_db().service_session(ServiceReason.HEALTH) as s:
            await s.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # health must report, not raise
        checks["postgres"] = f"error: {type(exc).__name__}"
    checks["redis"] = "ok" if await get_redis().ping() else "degraded"  # Redis is disposable: degraded, not down
    healthy = checks["postgres"] == "ok"
    return JSONResponse({"status": "ok" if healthy else "error", "checks": checks}, status_code=200 if healthy else 503)
