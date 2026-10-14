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
import reflexo.gateway.api as gateway
import reflexo.voice.api as voice
from reflexo.common.config import get_settings

router = APIRouter(prefix="/admin/api")


def problem(status: int, code: str, detail: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": code, "status": status, "code": code, "detail": detail},
        headers=headers,
    )


async def problem_handler(_: Request, exc: HTTPException) -> JSONResponse:
    body = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"title": str(exc.detail), "status": exc.status_code, "code": "ERROR", "detail": str(exc.detail)}
    )
    return JSONResponse(body, status_code=exc.status_code, media_type="application/problem+json", headers=exc.headers)


# ---------------------------------------------------------------- session


def session(request: Request) -> admin.Session:
    """The web app says which surface it is (the ops console, or an owner's app/demo); each has its own cookie, so
    an operator and an owner can be signed in side by side in one browser."""
    surface = request.headers.get(admin.SURFACE_HEADER)
    s = admin.verify_session(request.cookies.get(admin.OWNER_COOKIE if surface == "app" else admin.COOKIE))
    if s is None:
        raise problem(401, "UNAUTHENTICATED", "login required")
    # deny by default: a reviewer may read and sign out; no other request reaches a handler
    if s.read_only and request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.endswith("/logout"):
        raise problem(403, "READ_ONLY", "reviewer access is read-only")
    return s


def csrf(request: Request, s: Annotated[admin.Session, Depends(session)]) -> admin.Session:
    if request.headers.get(admin.CSRF_HEADER) != s.csrf:
        raise problem(403, "CSRF_FAILED", "missing or invalid X-CSRF-Token")
    return s


Authed = Annotated[admin.Session, Depends(session)]
Mutating = Annotated[admin.Session, Depends(csrf)]


def operator(s: Authed) -> admin.Session:
    """Engineering tools (X-ray stream, chaos, scenarios, evals, settings, demo reset) are for the operator only."""
    if not s.is_operator:
        raise problem(403, "FORBIDDEN", "operator only")
    return s


def operator_mutating(s: Mutating) -> admin.Session:
    return operator(s)


Operator = Annotated[admin.Session, Depends(operator)]
OperatorMutating = Annotated[admin.Session, Depends(operator_mutating)]


async def visible_businesses(s: admin.Session) -> list[dict[str, Any]]:
    every = await admin.businesses()
    if s.read_only:
        allowed = admin.reviewer_slugs()
        return [b for b in every if b["slug"] in allowed]
    return every if s.is_operator else [b for b in every if b["id"] == s.business_id]


async def business(business_id: Annotated[UUID, Query()], s: Authed) -> UUID:
    if s.business_id is not None and str(business_id) != s.business_id:
        raise problem(404, "NOT_FOUND", "unknown business")
    if str(business_id) not in {b["id"] for b in await visible_businesses(s)}:
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
    if admin.check_credentials(body.username, body.password):
        token, s = admin.issue_session(body.username)
    elif admin.check_reviewer(body.username, body.password):
        token, s = admin.issue_session(body.username, read_only=True)
    else:
        admin.record_login_failure(client)
        raise problem(401, "INVALID_CREDENTIALS", "wrong username or password")
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
    return {"user": s.user, "csrf": s.csrf, "expires_at": s.expires_at, "read_only": s.read_only}


@router.post("/logout")
async def logout(response: Response, s: Mutating) -> dict[str, bool]:
    response.delete_cookie(admin.COOKIE if s.is_operator else admin.OWNER_COOKIE, path="/admin")
    return {"ok": True}


@router.get("/me")
async def me(s: Authed) -> dict[str, Any]:
    settings = get_settings()
    return {
        "user": s.user,
        "role": "operator" if s.is_operator else "owner",
        "business_id": s.business_id,
        "read_only": s.read_only,
        "csrf": s.csrf,
        "expires_at": s.expires_at,
        "env": settings.env,
        "demo_time_scale": settings.demo_time_scale,
        "links": {"jaeger": settings.jaeger_ui_url, "temporal": settings.temporal_ui_url},
    }


# ---------------------------------------------------------------- overview & X-ray


@router.get("/health")
async def health(_: Operator) -> dict[str, Any]:
    return await admin.health()


@router.get("/businesses")
async def businesses(s: Authed) -> list[dict[str, Any]]:
    return await visible_businesses(s)


@router.get("/stream")
async def stream(_: Operator, business_id: Business) -> EventSourceResponse:
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


# ---------------------------------------------------------------- chaos (POC_07 §5)


@router.get("/chaos")
async def chaos_overview(_: Operator, business_id: Business) -> dict[str, Any]:
    return {
        "actions": [{"kind": k, "expected": admin.chaos.EXPECTED[k]} for k in admin.chaos.KINDS],
        "flags": await admin.chaos.active_flags(),
        "recent": await admin.recent_chaos(business_id),
    }


@router.post("/chaos/{kind}", status_code=202)
async def chaos_start(kind: str, _: OperatorMutating, business_id: Business) -> dict[str, Any]:
    """Starts the action and returns at once; poll GET /chaos for the verified outcome."""
    if kind not in admin.chaos.EXPECTED:
        raise problem(404, "NOT_FOUND", f"unknown chaos action {kind}")
    try:
        run_id = await admin.chaos.start(kind, business_id)
    except admin.chaos.ChaosBusy as busy:
        raise problem(409, "CHAOS_BUSY", f"{busy} is still running; wait for it to finish") from None
    return {"id": str(run_id), "kind": kind, "state": "running"}


@router.post("/chaos/flags/{key}/clear")
async def chaos_clear_flag(key: str, _: OperatorMutating) -> dict[str, Any]:
    try:
        cleared = await admin.chaos.clear_flag(key)
    except ValueError:
        raise problem(404, "NOT_FOUND", f"{key} is not a chaos flag") from None
    return {"key": key, "cleared": cleared}


# ---------------------------------------------------------------- simulated actors (POC_07 §3.2)


SIM_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
SIM_UPLOAD_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")


@router.post("/sim/upload")
async def sim_upload(request: Request, _: Mutating, business_id: Business) -> dict[str, Any]:
    """A photo or PDF picked on a simulated phone (raw body, its type in Content-Type); send it with /sim/send."""
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in SIM_UPLOAD_TYPES:
        raise problem(415, "UNSUPPORTED_MEDIA", "send a JPEG, PNG or WebP photo, or a PDF")
    content = await request.body()
    if not content or len(content) > SIM_UPLOAD_MAX_BYTES:
        raise problem(413, "TOO_LARGE", "files up to 8 MB")
    media_id = await gateway.store_simulated_upload(business_id, content, mime)
    return {"media_id": str(media_id), "mime": mime}


class SimMediaRef(BaseModel):
    id: UUID
    mime: str = Field(max_length=100)
    filename: str | None = Field(default=None, max_length=200)

    def sim(self) -> admin.sim.SimMedia:
        return admin.sim.SimMedia(self.id, self.mime, self.filename)


class SimSendBody(BaseModel):
    actor: int = Field(ge=1, le=9999)
    name: str | None = Field(default=None, max_length=60)
    text: str | None = Field(default=None, max_length=4000)
    option_id: str | None = Field(default=None, max_length=100)
    option_title: str | None = Field(default=None, max_length=100)
    media: SimMediaRef | None = None


@router.post("/sim/send")
async def sim_send(body: SimSendBody, _: Mutating, business_id: Business) -> dict[str, Any]:
    if not body.text and not body.option_id and not body.media:
        raise problem(422, "VALIDATION_FAILED", "text, media or option_id required")
    result = await admin.sim.customer_says(
        business_id,
        body.actor,
        text_=body.text,
        option_id=body.option_id,
        option_title=body.option_title,
        name=body.name,
        media=body.media.sim() if body.media else None,
    )
    return {"status": result.status, "external_message_id": result.external_message_id, "wa_id": result.wa_id}


@router.get("/sim/thread")
async def sim_thread(actor: Annotated[int, Query(ge=1, le=9999)], _: Authed, business_id: Business) -> dict[str, Any]:
    return await admin.sim.thread(business_id, actor)


@router.post("/sim/razorpay/pay/{payment_link_id}")
async def sim_pay(payment_link_id: UUID, _: Mutating, business_id: Business) -> dict[str, int]:
    return {"status": await admin.sim.pay_link(business_id, payment_link_id)}


class SimCallBody(BaseModel):
    actor: int = Field(ge=1, le=9999)
    name: str | None = Field(default=None, max_length=60)


@router.post("/sim/call")
async def sim_call(body: SimCallBody, _: Mutating, business_id: Business) -> dict[str, Any]:
    """Call the business from the browser as a simulated customer: the real voice agent, joined by microphone."""
    try:
        call = await voice.web_call.start(business_id, admin.sim.actor_wa_id(body.actor), body.name)
    except voice.WebCallUnavailable as exc:
        raise problem(503, "VOICE_UNAVAILABLE", str(exc)) from None
    return {"call_session_id": str(call.call_session_id), "room": call.room, "url": call.url, "token": call.token}


class SimOwnerBody(BaseModel):
    text: str | None = Field(default=None, max_length=4000)
    option_id: str | None = Field(default=None, max_length=100)
    option_title: str | None = Field(default=None, max_length=100)
    media: SimMediaRef | None = None


@router.post("/sim/owner/send")
async def sim_owner_send(body: SimOwnerBody, _: Mutating, business_id: Business) -> dict[str, Any]:
    """The Lab's owner device (simulated owner binding)."""
    if not body.text and not body.option_id and not body.media:
        raise problem(422, "VALIDATION_FAILED", "text, media or option_id required")
    try:
        result = await admin.sim.owner_says(
            business_id,
            text_=body.text,
            option_id=body.option_id,
            option_title=body.option_title,
            media=body.media.sim() if body.media else None,
        )
    except LookupError as exc:
        raise problem(409, "NO_SIM_OWNER", str(exc)) from None
    return {"status": result.status, "external_message_id": result.external_message_id}


@router.get("/sim/owner/thread")
async def sim_owner_thread(_: Authed, business_id: Business) -> dict[str, Any]:
    return await admin.sim.owner_thread(business_id)


# ---------------------------------------------------------------- Lab scenarios (POC_07 §3.3)


@router.get("/scenarios")
async def scenarios_list(_: Operator) -> dict[str, Any]:
    return {"scenarios": admin.scenarios.catalogue(), "runs": admin.scenarios.recent()}


@router.post("/scenarios/{scenario_id}/run", status_code=202)
async def scenario_run(scenario_id: str, _: OperatorMutating, business_id: Business) -> dict[str, Any]:
    try:
        run = admin.scenarios.start(scenario_id, business_id)
    except LookupError:
        raise problem(404, "NOT_FOUND", f"unknown scenario {scenario_id}") from None
    return {"id": run.id, "status": run.status}


@router.get("/scenarios/runs/{run_id}")
async def scenario_run_detail(run_id: str, _: Operator) -> dict[str, Any]:
    run = admin.scenarios.get(run_id)
    if run is None:
        raise problem(404, "NOT_FOUND", "unknown run")
    return run


@router.post("/scenarios/runs/{run_id}/stop")
async def scenario_stop(run_id: str, _: OperatorMutating) -> dict[str, bool]:
    return {"stopped": admin.scenarios.stop(run_id)}


# ---------------------------------------------------------------- workspace pages (POC_07 §2, WP-13a)


@router.get("/orders")
async def orders(
    _: Authed,
    business_id: Business,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    try:
        return await admin.workspace.orders(business_id, status, limit)
    except ValueError as exc:
        raise problem(422, "INVALID", str(exc)) from exc


@router.get("/orders/capacity")
async def orders_capacity(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.capacity(business_id)


@router.get("/orders/{order_id}")
async def order(order_id: UUID, _: Authed, business_id: Business) -> dict[str, Any]:
    found = await admin.workspace.order_detail(business_id, order_id)
    if found is None:
        raise problem(404, "NOT_FOUND", "order not found")
    return found


@router.get("/automations")
async def automations(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.automations(business_id)


@router.get("/automations/{automation_id}")
async def automation(automation_id: UUID, _: Authed, business_id: Business) -> dict[str, Any]:
    found = await admin.workspace.automation_detail(business_id, automation_id)
    if found is None:
        raise problem(404, "NOT_FOUND", "automation not found")
    return found


async def _set_automation(business_id: UUID, automation_id: UUID, status: str) -> dict[str, str]:
    try:
        return {"status": await admin.workspace.set_automation_status(business_id, automation_id, status)}
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc


@router.post("/automations/{automation_id}/pause")
async def automation_pause(automation_id: UUID, _: Mutating, business_id: Business) -> dict[str, str]:
    return await _set_automation(business_id, automation_id, "paused")


@router.post("/automations/{automation_id}/resume")
async def automation_resume(automation_id: UUID, _: Mutating, business_id: Business) -> dict[str, str]:
    return await _set_automation(business_id, automation_id, "active")


@router.post("/automation-runs/{run_id}/skip-wait")
async def automation_skip_wait(run_id: UUID, _: Mutating, business_id: Business) -> dict[str, bool]:
    try:
        return {"sent": await admin.workspace.skip_wait(business_id, run_id)}
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc


@router.get("/playbook")
async def playbook(_: Authed, business_id: Business) -> dict[str, Any]:
    return await admin.workspace.playbook(business_id)


@router.get("/owner")
async def owner_workspace(_: Authed, business_id: Business) -> dict[str, Any]:
    return await admin.workspace.owner(business_id)


@router.get("/catalogue/change-sets")
async def change_sets(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.change_sets(business_id)


@router.post("/catalogue/change-sets/{change_set_id}/revert")
async def change_set_revert(change_set_id: UUID, _: Mutating, business_id: Business) -> dict[str, Any]:
    try:
        return await admin.workspace.revert_change_set(business_id, change_set_id)
    except LookupError as exc:
        raise problem(404, "NOT_FOUND", str(exc)) from exc
    except ValueError as exc:  # already reverted / still a draft
        raise problem(409, "NOT_APPLIED", str(exc)) from exc
    except admin.workspace.ChangeConflict as exc:
        raise problem(409, "CONFLICT", str(exc)) from exc


@router.get("/imports")
async def imports(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.imports(business_id)


@router.get("/calls")
async def calls(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.calls(business_id)


@router.get("/settings")
async def settings_view(_: Operator, business_id: Business) -> dict[str, Any]:
    return await admin.workspace.settings_view(business_id)


@router.post("/demo-reset")
async def demo_reset(
    _: OperatorMutating, mode: Annotated[str, Query(pattern="^(onboarding|full)$")] = "onboarding"
) -> dict[str, Any]:
    """`make demo-reset [FULL=1]` from the Settings page: every demo business is wiped and reseeded (new ids)."""
    try:
        return await admin.demo_reset(mode)
    except RuntimeError as exc:
        raise problem(409, "BUSY", str(exc)) from exc


@router.get("/customers")
async def customers(_: Authed, business_id: Business) -> list[dict[str, Any]]:
    return await admin.workspace.customers(business_id)


@router.get("/customers/{customer_id}")
async def customer(customer_id: UUID, _: Authed, business_id: Business) -> dict[str, Any]:
    found = await admin.workspace.customer_detail(business_id, customer_id)
    if found is None:
        raise problem(404, "NOT_FOUND", "customer not found")
    return found


# ---------------------------------------------------------------- evals (POC_08 §4)


@router.get("/evals")
async def evals_overview(_: Operator) -> dict[str, Any]:
    return {"runs": await admin.evals.recent(), **admin.evals.status()}


@router.post("/evals/run", status_code=202)
async def evals_run(_: OperatorMutating, suite: str | None = None) -> dict[str, Any]:
    """Start a run in the API process (one at a time); progress via GET /evals."""
    suites = None if suite in (None, "", "all") else [s.strip() for s in suite.split(",")]
    if suites and (unknown := set(suites) - set(admin.evals.ORDER)):
        raise problem(422, "INVALID", f"unknown suite(s): {sorted(unknown)}")
    if not admin.evals.start(suites):
        raise problem(409, "BUSY", "an eval run is already in progress")
    return {"started": True, "suites": suites or admin.evals.ORDER}


@router.get("/onboarding")
async def onboarding_view(_: Authed, business_id: Business) -> dict[str, Any]:
    """Where the business is in getting set up: the owner app's checklist."""
    return await admin.workspace.onboarding_view(business_id)


@router.get("/knowledge")
async def knowledge_view(_: Authed, business_id: Business) -> dict[str, Any]:
    return await admin.workspace.knowledge_view(business_id)
