"""Webhook ingress routes (POC_03 §2.1). Thin: verification, dedupe and storage live in `gateway.api`."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

import reflexo.gateway.api as gateway

router = APIRouter()

META_PROVIDERS = ("meta_whatsapp", "meta_pages", "meta_instagram")


async def _ingest(provider: str, request: Request, path_params: dict[str, str]) -> Response:
    raw = await request.body()
    result = await gateway.ingest_webhook(provider, dict(request.headers), raw, path_params)
    media_type = "application/json" if provider == "telegram" else "text/plain"
    return Response(content=result.body, status_code=result.status_code, media_type=media_type)


for _provider in META_PROVIDERS:

    def _make(provider: str):  # type: ignore[no-untyped-def]
        async def verify(
            mode: str | None = Query(None, alias="hub.mode"),
            token: str | None = Query(None, alias="hub.verify_token"),
            challenge: str | None = Query(None, alias="hub.challenge"),
        ) -> Response:
            answer = gateway.verify_meta_challenge(mode, token, challenge)
            if answer is None:
                return PlainTextResponse("forbidden", status_code=403)
            return PlainTextResponse(answer)

        async def receive(request: Request) -> Response:
            return await _ingest(provider, request, {})

        return verify, receive

    _verify, _receive = _make(_provider)
    router.add_api_route(f"/webhooks/{_provider}", _verify, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"/webhooks/{_provider}", _receive, methods=["POST"], include_in_schema=False)


@router.post("/webhooks/telegram/{connection_id}", include_in_schema=False)
async def telegram(connection_id: str, request: Request) -> Response:
    return await _ingest("telegram", request, {"connection_id": connection_id})


@router.post("/webhooks/simulator/{connection_id}", include_in_schema=False)
async def simulator(connection_id: str, request: Request) -> Response:
    return await _ingest("simulator", request, {"connection_id": connection_id})
