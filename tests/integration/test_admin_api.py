"""Admin API (POC_07 §4, §6): auth + CSRF (CHK-ADM-1), X-ray traces and stream incl. Redis-down polling
(CHK-F11-2), business isolation (CHK-F12-2), conversations with reply / takeover / release."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

import reflexo.admin.api as admin
import reflexo.gateway.api as gateway_api
from reflexo.api.main import app
from reflexo.common.db import Database, ServiceReason
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.ports import Registry, reset_registry
from reflexo.common.tracing import configure_tracing, tracer
from reflexo.common.xray import emit
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_gateway import deliver, make_shop, wa_message

pytestmark = pytest.mark.integration


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    return reg


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient) -> str:
    resp = await client.post("/admin/api/login", json={"username": "admin", "password": "test-admin-password"})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["csrf"])


async def emit_trace(business_id: UUID, conversation_id: UUID | None = None) -> str:
    configure_tracing("test", None, "test")
    with tracer().start_as_current_span("test-trace") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        await emit("inbox", PipelineEventStatus.OK, provider="meta_whatsapp", simulated=True)  # no business yet
        await emit(
            "identity",
            PipelineEventStatus.OK,
            business_id=business_id,
            conversation_id=conversation_id,
            duration_ms=4,
            role="customer",
        )
        await emit(
            "model_call",
            PipelineEventStatus.OK,
            business_id=business_id,
            duration_ms=900,
            model="gemini-2.5-flash-lite",
            hedged=True,
            input_tokens=1200,
            output_tokens=60,
        )
        await emit("gate", PipelineEventStatus.OK, business_id=business_id, duration_ms=1, passed=True)
        await emit("reply", PipelineEventStatus.OK, business_id=business_id, duration_ms=1500, action="send")
    return trace_id


# ---------------------------------------------------------------- auth (CHK-ADM-1)


async def test_auth_and_csrf(client: httpx.AsyncClient, db: Database) -> None:
    assert (await client.get("/admin/api/me")).status_code == 401
    bad = await client.post("/admin/api/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401 and bad.headers["content-type"].startswith("application/problem+json")
    assert bad.json()["code"] == "INVALID_CREDENTIALS"
    csrf = await login(client)
    me = await client.get("/admin/api/me")
    assert me.status_code == 200 and me.json()["csrf"] == csrf
    # a mutating request without the CSRF header is refused even with a valid session
    assert (await client.post("/admin/api/logout")).status_code == 403
    assert (await client.post("/admin/api/logout", headers={"X-CSRF-Token": "forged"})).status_code == 403
    assert (await client.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})).status_code == 200


async def test_tampered_cookie_rejected(client: httpx.AsyncClient, db: Database) -> None:
    await login(client)
    token = client.cookies.get(admin.COOKIE)
    assert token
    payload, sig = token.rsplit(".", 1)
    client.cookies.set(admin.COOKIE, payload + "." + ("0" * len(sig)), path="/admin")
    assert (await client.get("/admin/api/me")).status_code == 401


def test_login_throttle() -> None:
    for _ in range(8):
        admin.record_login_failure("10.0.0.9")
    assert admin.login_throttled("10.0.0.9")
    assert not admin.login_throttled("10.0.0.10")


# ---------------------------------------------------------------- X-ray


async def test_traces_list_detail_and_isolation(client: httpx.AsyncClient, db: Database) -> None:
    a, b = await make_shop(db), await make_shop(db)
    trace_id = await emit_trace(a.business_id)
    await login(client)

    items = (await client.get("/admin/api/traces", params={"business_id": str(a.business_id)})).json()["items"]
    mine = next(t for t in items if t["trace_id"] == trace_id)
    assert (mine["outcome"], mine["model"], mine["hedged"], mine["gate"], mine["simulated"]) == (
        "send",
        "gemini-2.5-flash-lite",
        True,
        True,
        True,
    )
    assert mine["stages"] == 5  # includes the pre-identity inbox event (business_id NULL, same trace)
    assert mine["provider"] == "meta_whatsapp"

    detail = (await client.get(f"/admin/api/traces/{trace_id}", params={"business_id": str(a.business_id)})).json()
    assert [e["stage"] for e in detail["events"]] == ["inbox", "identity", "model_call", "gate", "reply"]
    assert all(e["start_ms"] >= 0 for e in detail["events"])
    reply = detail["events"][-1]
    assert reply["duration_ms"] == 1500 and reply["attrs"]["action"] == "send"

    # business B sees neither the list entry nor the detail
    other = (await client.get("/admin/api/traces", params={"business_id": str(b.business_id)})).json()["items"]
    assert all(t["trace_id"] != trace_id for t in other)
    assert (
        await client.get(f"/admin/api/traces/{trace_id}", params={"business_id": str(b.business_id)})
    ).status_code == 404
    assert (
        await client.get("/admin/api/traces", params={"business_id": "00000000-0000-0000-0000-000000000000"})
    ).status_code == 404


async def test_stream_backfill_and_polling_when_redis_down(db: Database) -> None:
    shop = await make_shop(db)
    await emit_trace(shop.business_id)
    gen = admin.xray_stream(shop.business_id)
    events = []
    async for item in gen:
        data = json.loads(item["data"]) if "data" in item else {}
        events.append(data)
        if data.get("type") == "mode":
            break
    assert [e["stage"] for e in events if e.get("type") == "xray"][-4:] == ["identity", "model_call", "gate", "reply"]
    assert {"type": "backfill_done"} in events
    assert events[-1] == {"type": "mode", "mode": "polling"}  # tests run with Redis disabled

    # a new event arrives through polling
    trace2 = await emit_trace(shop.business_id)
    seen = []
    async for item in gen:
        data = json.loads(item.get("data", "{}"))
        if data.get("type") == "xray":
            seen.append(data["trace_id"])
        if seen.count(trace2) >= 4:
            break
    await gen.aclose()
    assert trace2 in seen


# ---------------------------------------------------------------- conversations


async def test_conversations_reply_takeover_release(
    client: httpx.AsyncClient, registry: Registry, db: Database
) -> None:
    shop = await make_shop(db)
    other = await make_shop(db)
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(shop.pnid, "919822200001", "wamid.A1", "Hello, my number is 9876543210"),
        shop.pnid,
    )
    csrf = await login(client)
    params = {"business_id": str(shop.business_id)}

    listing = (await client.get("/admin/api/conversations", params=params)).json()
    [conv] = listing["items"]
    assert conv["channel"] == "whatsapp" and "9876543210" not in conv["last_text"]  # phones masked
    assert (await client.get("/admin/api/conversations", params={"business_id": str(other.business_id)})).json()[
        "items"
    ] == []

    thread = (await client.get(f"/admin/api/conversations/{conv['id']}", params=params)).json()
    assert thread["messages"][0]["direction"] == "inbound"
    assert (
        await client.get(f"/admin/api/conversations/{conv['id']}", params={"business_id": str(other.business_id)})
    ).status_code == 404

    no_csrf = await client.post(f"/admin/api/conversations/{conv['id']}/reply", params=params, json={"text": "hi"})
    assert no_csrf.status_code == 403
    h = {"X-CSRF-Token": csrf}
    sent = await client.post(
        f"/admin/api/conversations/{conv['id']}/reply", params=params, json={"text": "Meera here 🙂"}, headers=h
    )
    assert sent.status_code == 200 and sent.json()["status"] == "queued"
    async with db.service_session(ServiceReason.ADMIN) as s:
        row = (
            await s.execute(
                text("SELECT author, content_text FROM messages WHERE id = :id"), {"id": sent.json()["message_id"]}
            )
        ).one()
        human = (
            await s.execute(
                text("SELECT human_active_until > now() FROM conversations WHERE id = :c"), {"c": conv["id"]}
            )
        ).scalar_one()
    assert (str(row.author), row.content_text, human) == ("team", "Meera here 🙂", True)

    assert (
        await client.post(f"/admin/api/conversations/{conv['id']}/release", params=params, headers=h)
    ).status_code == 200
    thread = (await client.get(f"/admin/api/conversations/{conv['id']}", params=params)).json()
    assert thread["human_active_until"] is None
    assert (
        await client.post(f"/admin/api/conversations/{conv['id']}/takeover", params=params, headers=h)
    ).status_code == 200
    thread = (await client.get(f"/admin/api/conversations/{conv['id']}", params=params)).json()
    assert thread["human_active_until"] is not None


async def test_health_and_businesses(client: httpx.AsyncClient, db: Database) -> None:
    shop = await make_shop(db)
    await login(client)
    health = (await client.get("/admin/api/health")).json()
    assert health["postgres"] == "ok" and health["redis"] == "degraded"
    assert {q["pool"] for q in health["queues"]} == {"inbound", "turns", "outbound", "jobs"}
    ids = {b["id"] for b in (await client.get("/admin/api/businesses")).json()}
    assert str(shop.business_id) in ids


# ---------------------------------------------------------------- the public demo: owner sessions on a sandbox


async def start_demo(client: httpx.AsyncClient) -> dict[str, str]:
    resp = await client.post("/api/demo/start")
    assert resp.status_code == 200, resp.text
    # the cookie is Secure outside dev; the ASGI test transport is plain http, so carry it by hand
    client.cookies.set(admin.OWNER_COOKIE, resp.cookies[admin.OWNER_COOKIE])
    return {"business_id": resp.json()["business_id"], "csrf": resp.json()["csrf"]}


async def test_demo_owner_is_locked_to_their_sandbox(client: httpx.AsyncClient, db: Database) -> None:
    admin.demo._created.clear()
    other_shop = (await make_shop(db)).business_id
    demo = await start_demo(client)
    app_headers = {admin.SURFACE_HEADER: "app", admin.CSRF_HEADER: demo["csrf"]}

    me = (await client.get("/admin/api/me", headers=app_headers)).json()
    assert (me["role"], me["business_id"]) == ("owner", demo["business_id"])
    shops = (await client.get("/admin/api/businesses", headers=app_headers)).json()
    assert [(b["id"], b["sandbox"]) for b in shops] == [(demo["business_id"], True)]
    own = await client.get("/admin/api/orders", params={"business_id": demo["business_id"]}, headers=app_headers)
    assert own.status_code == 200
    # another business, the engineering tools, and the operator surface are all closed to an owner
    assert (
        await client.get("/admin/api/orders", params={"business_id": str(other_shop)}, headers=app_headers)
    ).status_code == 404
    assert (await client.get("/admin/api/evals", headers=app_headers)).status_code == 403
    assert (await client.post("/admin/api/demo-reset", headers=app_headers)).status_code == 403
    assert (await client.get("/admin/api/me")).status_code == 401  # the ops console needs the operator cookie

    # the operator, signed in alongside in the same browser, still sees everything
    csrf = await login(client)
    every = (await client.get("/admin/api/businesses", headers={admin.CSRF_HEADER: csrf})).json()
    assert {demo["business_id"], str(other_shop)} <= {b["id"] for b in every}


async def test_demo_limits_and_expiry(client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from reflexo.common.config import get_settings

    admin.demo._created.clear()
    monkeypatch.setattr(get_settings(), "demo_per_client_per_hour", 1)
    first = await start_demo(client)
    again = await client.post("/api/demo/start")
    assert again.status_code == 429 and int(again.headers["retry-after"]) > 0

    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE businesses SET created_at = now() - interval '25 hours' WHERE id = :b"),
            {"b": UUID(first["business_id"])},
        )
    assert await admin.demo.expire() >= 1
    async with db.service_session(ServiceReason.ADMIN) as s:
        gone = (
            await s.execute(text("SELECT count(*) FROM businesses WHERE id = :b"), {"b": UUID(first["business_id"])})
        ).scalar_one()
    assert gone == 0


# ---------------------------------------------------------------- self-serve sign-up and log-in


SIGNUP = {
    "email": "Owner@Sharma-Sweets.in",
    "password": "laddoo-2026",
    "business_name": "Sharma Sweets",
    "owner_name": "Ravi",
    "category": "sweet shop",
    "area": "Indore",
    "accept_terms": True,
}


async def test_signup_creates_a_shop_in_onboarding_and_signs_the_owner_in(
    client: httpx.AsyncClient, db: Database
) -> None:
    admin.accounts._signups.clear()
    resp = await client.post("/api/accounts/signup", json=SIGNUP)
    assert resp.status_code == 200, resp.text
    business_id = resp.json()["business_id"]
    client.cookies.set(admin.OWNER_COOKIE, resp.cookies[admin.OWNER_COOKIE])
    headers = {admin.SURFACE_HEADER: "app", admin.CSRF_HEADER: resp.json()["csrf"]}

    me = (await client.get("/admin/api/me", headers=headers)).json()
    assert (me["role"], me["business_id"], me["user"]) == ("owner", business_id, "Ravi")
    setup = (await client.get("/admin/api/onboarding", params={"business_id": business_id}, headers=headers)).json()
    assert (setup["state"], setup["business_status"], setup["products"]) == ("sources_requested", "onboarding", 0)
    async with db.service_session(ServiceReason.ADMIN) as s:
        shop = (
            await s.execute(
                text("SELECT slug, settings->>'order_number_prefix' AS prefix FROM businesses WHERE id = :b"),
                {"b": UUID(business_id)},
            )
        ).one()
        welcome = (
            await s.execute(
                text("""SELECT c.simulated FROM outbound_messages o JOIN conversations c ON c.id = o.conversation_id
                        WHERE o.business_id = :b AND c.participant_role = 'owner'"""),
                {"b": UUID(business_id)},
            )
        ).all()
    assert shop.slug.startswith("sharma-sweets-") and shop.prefix == "SS"
    assert welcome and all(w.simulated for w in welcome)  # the AI greets on the owner's (simulated) phone
    async with db.service_session(ServiceReason.ADMIN) as s:
        [consent] = (
            await s.execute(
                text("SELECT purpose, granted, method, evidence FROM consent_records WHERE business_id = :b"),
                {"b": UUID(business_id)},
            )
        ).all()
    assert (consent.purpose, consent.granted, consent.method) == ("service_terms", True, "signup_terms")
    assert consent.evidence["seller_terms"].startswith("seller-terms-")  # the version the owner accepted

    again = await client.post("/api/accounts/signup", json=SIGNUP | {"email": "owner@sharma-sweets.in"})
    assert again.status_code == 409  # emails are case-insensitive

    client.cookies.clear()
    wrong = await client.post("/api/accounts/login", json={"email": SIGNUP["email"], "password": "nope-nope"})
    assert wrong.status_code == 401
    ok = await client.post("/api/accounts/login", json={"email": "owner@sharma-sweets.in", "password": "laddoo-2026"})
    assert ok.status_code == 200 and ok.json()["business_id"] == business_id


async def test_signup_needs_the_terms_accepted(client: httpx.AsyncClient) -> None:
    admin.accounts._signups.clear()
    resp = await client.post("/api/accounts/signup", json=SIGNUP | {"email": "a@terms.in", "accept_terms": False})
    assert resp.status_code == 422 and "Seller Terms" in resp.text


async def test_signup_validates_and_limits(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflexo.common.config import get_settings

    admin.accounts._signups.clear()
    assert (await client.post("/api/accounts/signup", json=SIGNUP | {"email": "not-an-email"})).status_code == 422
    assert (await client.post("/api/accounts/signup", json=SIGNUP | {"password": "short"})).status_code == 422
    monkeypatch.setattr(get_settings(), "signup_per_client_per_hour", 1)
    first = await client.post("/api/accounts/signup", json=SIGNUP | {"email": "a@limits.in"})
    second = await client.post("/api/accounts/signup", json=SIGNUP | {"email": "b@limits.in"})
    assert first.status_code == 200 and second.status_code == 429


def test_password_hashes_are_salted_and_verify() -> None:
    one, two = admin.accounts.hash_password("secret-pass"), admin.accounts.hash_password("secret-pass")
    assert one != two and one.startswith("scrypt$")
    assert admin.accounts.verify_password("secret-pass", one) and not admin.accounts.verify_password("wrong", one)
    assert not admin.accounts.verify_password("secret-pass", "garbage")


async def test_reviewer_is_read_only_and_sees_only_showcase_businesses(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflexo.common.config import get_settings

    shown, hidden = await make_shop(db), await make_shop(db)
    monkeypatch.setenv("REFLEXO_REVIEWER_USER", "reviewer")
    monkeypatch.setenv("REFLEXO_REVIEWER_PASSWORD", "public-review-pass")
    monkeypatch.setenv("REFLEXO_REVIEWER_BUSINESSES", f"shop-{shown.business_id}")
    get_settings.cache_clear()
    try:
        resp = await client.post("/admin/api/login", json={"username": "reviewer", "password": "public-review-pass"})
        assert resp.status_code == 200 and resp.json()["read_only"] is True
        csrf = resp.json()["csrf"]
        me = (await client.get("/admin/api/me")).json()
        assert me["role"] == "operator" and me["read_only"] is True

        # it sees the showcase business and nothing else, even by asking for another id directly
        ids = {b["id"] for b in (await client.get("/admin/api/businesses")).json()}
        assert ids == {str(shown.business_id)}
        orders = "/admin/api/orders"
        assert (await client.get(orders, params={"business_id": str(shown.business_id)})).status_code == 200
        assert (await client.get(orders, params={"business_id": str(hidden.business_id)})).status_code == 404
        assert (await client.get("/admin/api/health")).status_code == 200

        # every change is refused before any handler runs, even with a valid CSRF token
        headers = {"X-CSRF-Token": csrf}
        for path in ("/admin/api/chaos/kill_worker", "/admin/api/evals/run", "/admin/api/demo-reset"):
            r = await client.post(path, params={"business_id": str(shown.business_id)}, headers=headers)
            assert r.status_code == 403 and r.json()["code"] == "READ_ONLY", path
        assert (await client.post("/admin/api/logout", headers=headers)).status_code == 200

        # the operator login is unaffected
        await login(client)
        assert (await client.get("/admin/api/me")).json()["read_only"] is False
    finally:
        get_settings.cache_clear()


async def test_reviewer_login_disabled_without_configuration(client: httpx.AsyncClient, db: Database) -> None:
    resp = await client.post("/admin/api/login", json={"username": "", "password": ""})
    assert resp.status_code == 401
