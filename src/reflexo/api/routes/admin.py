"""Admin API (`/admin/api`, POC_07 §6): cookie session + CSRF on mutations, RFC 9457 problem responses."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import reflexo.admin.api as admin
from reflexo.common.config import get_settings

router = APIRouter(prefix="/admin/api")


def problem(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": code, "status": status, "code": code, "detail": detail},
    )


async def problem_handler(_: Request, exc: HTTPException) -> JSONResponse:
    body = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"title": str(exc.detail), "status": exc.status_code, "code": "ERROR", "detail": str(exc.detail)}
    )
    return JSONResponse(body, status_code=exc.status_code, media_type="application/problem+json")


# ---------------------------------------------------------------- session


def session(request: Request) -> admin.Session:
    s = admin.verify_session(request.cookies.get(admin.COOKIE))
    if s is None:
        raise problem(401, "UNAUTHENTICATED", "login required")
    return s


def csrf(request: Request, s: Annotated[admin.Session, Depends(session)]) -> admin.Session:
    if request.headers.get(admin.CSRF_HEADER) != s.csrf:
        raise problem(403, "CSRF_FAILED", "missing or invalid X-CSRF-Token")
    return s


Authed = Annotated[admin.Session, Depends(session)]
Mutating = Annotated[admin.Session, Depends(csrf)]


async def business(business_id: Annotated[UUID, Query()]) -> UUID:
    if str(business_id) not in {b["id"] for b in await admin.businesses()}:
        raise problem(404, "NOT_FOUND", "unknown business")
    return business_id


Business = Annotated[UUID, Depends(business)]


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    if admin.login_throttled(client):
        raise problem(429, "RATE_LIMITED", "too many attempts; try again in a few minutes")
    if not admin.check_credentials(body.username, body.password):
        admin.record_login_failure(client)
        raise problem(401, "INVALID_CREDENTIALS", "wrong username or password")
    token, s = admin.issue_session(body.username)
    settings = get_settings()
    response.set_cookie(
        admin.COOKIE,
        token,
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        secure=settings.env != "dev",
        samesite="strict",
        path="/admin",
    )
    return {"user": s.user, "csrf": s.csrf, "expires_at": s.expires_at}


@router.post("/logout")
async def logout(response: Response, _: Mutating) -> dict[str, bool]:
    response.delete_cookie(admin.COOKIE, path="/admin")
    return {"ok": True}


@router.get("/me")
async def me(s: Authed) -> dict[str, Any]:
    settings = get_settings()
    return {
        "user": s.user,
        "csrf": s.csrf,
        "expires_at": s.expires_at,
        "env": settings.env,
        "demo_time_scale": settings.demo_time_scale,
        "links": {"jaeger": "/jaeger", "temporal": "/temporal"},
    }


# ---------------------------------------------------------------- overview & X-ray


@router.get("/health")
async def health(_: Authed) -> dict[str, Any]:
    return await admin.health()


@router.get("/businesses")
async def businesses(_: Authed) -> list[dict[str, Any]]:
    return await admin.businesses()


@router.get("/stream")
async def stream(_: Authed, business_id: Business) -> EventSourceResponse:
    return EventSourceResponse(admin.xray_stream(business_id), ping=15)


@router.get("/traces")
async def traces(_: Authed, business_id: Business, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return {"items": await admin.traces(business_id, limit)}


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str, _: Authed, business_id: Business) -> dict[str, Any]:
    detail = await admin.trace_detail(business_id, trace_id)
    if detail is None:
        raise problem(404, "NOT_FOUND", "trace not found for this business")
    return detail


# ---------------------------------------------------------------- conversations


@router.get("/conversations")
async def conversations(
    _: Authed,
    business_id: Business,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    before = datetime.fromisoformat(cursor) if cursor else None
    return await admin.conversations(business_id, limit, before)


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: UUID, _: Authed, business_id: Business) -> dict[str, Any]:
    thread = await admin.conversation_thread(business_id, conversation_id)
    if thread is None:
        raise problem(404, "NOT_FOUND", "conversation not found")
    return thread


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/conversations/{conversation_id}/reply")
async def reply(conversation_id: UUID, body: ReplyBody, _: Mutating, business_id: Business) -> dict[str, Any]:
    try:
        return await admin.reply_as_team(business_id, conversation_id, body.text)
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc


@router.post("/conversations/{conversation_id}/takeover")
async def takeover(conversation_id: UUID, _: Mutating, business_id: Business) -> dict[str, bool]:
    try:
        await admin.set_takeover(business_id, conversation_id, True)
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc
    return {"ok": True}


@router.post("/conversations/{conversation_id}/release")
async def release(conversation_id: UUID, _: Mutating, business_id: Business) -> dict[str, bool]:
    try:
        await admin.set_takeover(business_id, conversation_id, False)
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc
    return {"ok": True}
