"""Privacy basics (WP-22a; BETA_10 §6, §7, §8.3, §13): Meta's callbacks (CHK-SEC-13), operator TOTP and the
access log (CHK-SEC-11), the notice in a customer's first reply (CHK-SEC-8), the public legal pages and the owner's
forwarded request."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
from reflexo.admin import access, auth
from reflexo.api.main import app
from reflexo.common.config import get_settings
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ports import Registry
from tests.integration import test_learning
from tests.integration.test_ask_owner import bakery, q, wa_number
from tests.integration.test_customer_desk import PRIMARY, outbound_texts, reply, tool_results, turn_for
from tests.integration.test_order_turns import call
from tests.integration.test_owner_desk import owner_turn, say_owner, steps

pytestmark = pytest.mark.integration
fake, registry = test_learning.fake, test_learning.registry
SECRET = "test_meta_app_secret"  # REFLEXO_META_APP_SECRET in tests/conftest.py


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def signed(payload: dict[str, Any], secret: str = SECRET) -> str:
    body = _b64(json.dumps(payload).encode())
    return f"{_b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}.{body}"


@pytest.fixture
async def client() -> Any:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def setting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def use(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(f"REFLEXO_{key.upper()}", value)
        get_settings.cache_clear()

    yield use
    monkeypatch.undo()
    get_settings.cache_clear()


async def _connected_by(db: Database, business_id: Any, meta_user: str) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""UPDATE channel_connections SET connect_meta = CAST(:m AS jsonb)
                    WHERE business_id = :b AND provider = 'meta_whatsapp'"""),
            {"m": json.dumps({"meta_user_id": meta_user}), "b": business_id},
        )


# ---------------------------------------------------------------- Meta callbacks (CHK-SEC-13)


@pytest.mark.check("CHK-SEC-13")
async def test_the_data_deletion_callback_refuses_a_bad_signature_and_records_a_good_one(
    registry: Registry, db: Database, client: httpx.AsyncClient
) -> None:
    shop = await bakery(db)
    await _connected_by(db, shop.bid, "fb-user-777")
    payload = {"algorithm": "HMAC-SHA256", "user_id": "fb-user-777", "issued_at": 1792300000}

    forged = await client.post("/meta/data-deletion", data={"signed_request": signed(payload, "wrong-secret")})
    assert forged.status_code == 400
    assert not await q(db, "SELECT id FROM data_requests WHERE business_id = :b", b=shop.bid)

    resp = await client.post("/meta/data-deletion", data={"signed_request": signed(payload)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [row] = await q(
        db,
        """SELECT id, kind, status, extract(epoch FROM due_at - requested_at) / 86400 AS within
           FROM data_requests WHERE business_id = :b""",
        b=shop.bid,
    )
    assert (str(row.kind), str(row.status), round(float(row.within))) == ("deletion", "received", 30)
    assert body["confirmation_code"] == str(row.id) and body["url"].endswith(f"/meta/deletion-status/{row.id}")
    status = await client.get(f"/meta/deletion-status/{row.id}")
    assert "received" in status.text and str(row.id) in status.text


async def test_a_user_we_know_nothing_about_gets_a_confirmation_and_no_rows(
    registry: Registry, db: Database, client: httpx.AsyncClient
) -> None:
    payload = {"algorithm": "HMAC-SHA256", "user_id": "fb-user-never-seen"}
    body = (await client.post("/meta/data-deletion", data={"signed_request": signed(payload)})).json()
    assert body["confirmation_code"].startswith("n-")
    assert "no data" in (await client.get(f"/meta/deletion-status/{body['confirmation_code']}")).text
    assert (await client.post("/meta/data-deletion", data={"signed_request": "garbage"})).status_code == 400


async def test_deauthorizing_disconnects_that_users_connections(
    registry: Registry, db: Database, client: httpx.AsyncClient
) -> None:
    shop = await bakery(db)
    await _connected_by(db, shop.bid, "fb-user-888")
    payload = {"algorithm": "HMAC-SHA256", "user_id": "fb-user-888"}
    resp = await client.post("/meta/deauthorize", data={"signed_request": signed(payload)})
    assert resp.json() == {"ok": True, "connections_disconnected": 1}
    [conn] = await q(db, "SELECT status FROM channel_connections WHERE business_id = :b AND provider = "
                         "'meta_whatsapp'", b=shop.bid)  # fmt: skip
    assert str(conn.status) == "disconnected"
    assert len(await q(db, "SELECT id FROM data_requests WHERE business_id = :b", b=shop.bid)) == 1


# ---------------------------------------------------------------- operator TOTP and access log (CHK-SEC-11)

TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.mark.check("CHK-SEC-11")
async def test_operator_login_needs_the_authenticator_code(client: httpx.AsyncClient, setting: Any) -> None:
    setting(operator_totp_required="true", operator_totp_secret=TOTP_SECRET)
    creds = {"username": "admin", "password": "test-admin-password"}
    auth._attempts.clear()
    assert (await client.post("/admin/api/login", json=creds)).status_code == 401
    assert (await client.post("/admin/api/login", json=creds | {"totp": "000000"})).status_code == 401
    ok = await client.post("/admin/api/login", json=creds | {"totp": auth.totp(TOTP_SECRET)})
    assert ok.status_code == 200, ok.text
    auth._attempts.clear()


@pytest.mark.check("CHK-SEC-11")
async def test_an_operator_viewing_a_conversation_is_logged(
    registry: Registry, db: Database, fake: agent.FakeProvider, client: httpx.AsyncClient
) -> None:
    shop = await bakery(db)
    fake.script(PRIMARY, lambda a: reply("send", "Namaste!", topic="greeting_smalltalk"))
    handle = await turn_for(registry, db, shop.pnid, wa_number(), "hi")
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    conv = handle.ref.conversation_id

    access.forget()
    login = await client.post("/admin/api/login", json={"username": "admin", "password": "test-admin-password"})
    params = {"business_id": str(shop.bid)}
    for _ in range(2):  # the console polls: one row per view per 10 minutes
        assert (await client.get(f"/admin/api/conversations/{conv}", params=params)).status_code == 200, login.text
    rows = await q(db, "SELECT operator, object_type, object_id FROM operator_access_log WHERE business_id = :b",
                   b=shop.bid)  # fmt: skip
    assert [(r.operator, r.object_type, r.object_id) for r in rows] == [("admin", "conversation", conv)]


# ---------------------------------------------------------------- the notice in the first reply (CHK-SEC-8)


def noticing(seen: list[str]) -> Any:
    """Replies without the notice first; once the gate says what is missing, adds it as given in the context."""

    def script(a: agent.Attempt) -> agent.FakeReply:
        prompt = "\n".join(m.content or "" for m in a.messages)
        seen.append(prompt)
        match = re.search(r"privacy notice, exactly: (\S+)", prompt)
        if match and any("first reply to this customer" in str(r).lower() for r in tool_results(a)):
            shop = re.search(r"say that you are (.+?)'s assistant", prompt)
            return reply("send", f"Namaste! I'm {shop.group(1)}'s assistant. Privacy: {match.group(1)}",  # type: ignore[union-attr]
                         topic="greeting_smalltalk")  # fmt: skip
        return reply("send", "Namaste! How can I help?", topic="greeting_smalltalk")

    return script


@pytest.mark.check("CHK-SEC-8")
async def test_the_first_reply_carries_the_notice_once_and_no_later_reply_does(
    registry: Registry, db: Database, fake: agent.FakeProvider, setting: Any
) -> None:
    setting(customer_notice_enabled="true")
    shop = await bakery(db)
    [biz] = await q(db, "SELECT name, slug FROM businesses WHERE id = :b", b=shop.bid)
    link = f"https://reflexo.test/{biz.slug}/privacy"
    seen: list[str] = []
    fake.script(PRIMARY, noticing(seen))
    wa = wa_number()
    first = await turn_for(registry, db, shop.pnid, wa, "hi")
    await registry.require(TurnRunnerPort).run_turn(first)  # type: ignore[type-abstract]
    conv = first.ref.conversation_id
    [sent] = await outbound_texts(db, conv)
    assert link in sent and biz.name in sent  # the gate held the reply until it carried the notice
    [customer] = await q(db, "SELECT notice_shown_at FROM customers c JOIN conversations v ON v.customer_id = c.id "
                             "WHERE v.id = :c", c=conv)  # fmt: skip
    assert customer.notice_shown_at is not None

    seen.clear()
    second = await turn_for(registry, db, shop.pnid, wa, "cake hai?")
    await registry.require(TurnRunnerPort).run_turn(second)  # type: ignore[type-abstract]
    texts = await outbound_texts(db, conv)
    assert len(texts) == 2 and link not in texts[-1]
    assert not any("FIRST REPLY TO THIS CUSTOMER" in p for p in seen)  # no later reply is asked for it


# ---------------------------------------------------------------- public pages and the owner's forwarded request


async def test_the_public_legal_pages_are_served(registry: Registry, db: Database, client: httpx.AsyncClient) -> None:
    shop = await bakery(db)
    [biz] = await q(db, "SELECT name, slug FROM businesses WHERE id = :b", b=shop.bid)
    for path in ("/privacy", "/terms", "/data-deletion", "/rights"):
        page = await client.get(path)
        assert page.status_code == 200 and "Draft, not legally reviewed" in page.text, path
    notice = await client.get(f"/{biz.slug}/privacy")
    assert notice.status_code == 200 and "{shop_name}" not in notice.text
    assert biz.name.replace("'", "&#x27;") in notice.text or biz.name in notice.text
    assert (await client.get("/no-such-shop/privacy")).status_code == 404


async def test_the_owner_can_pass_on_a_customers_data_request(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    from reflexo.common.contracts import DataRequestPort
    from reflexo.workflows.api import DataRequests

    registry.provide(DataRequestPort, DataRequests())  # type: ignore[type-abstract]  # as bootstrap does
    shop = await bakery(db)
    fake.script(PRIMARY, lambda a: reply("send", "Namaste!", topic="greeting_smalltalk"))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa_number(), "hi"))  # type: ignore[type-abstract]
    [customer] = await q(db, "SELECT id, phone_e164 FROM customers WHERE business_id = :b", b=shop.bid)
    fake.script(
        PRIMARY,
        steps(call("privacy_request", customer_ref=customer.phone_e164, kind="deletion", what="wants it all gone"),
              lambda a: say_owner("Recorded.")),
    )  # fmt: skip
    await owner_turn(registry, db, shop, "is customer ka data delete karna hai")
    [row] = await q(db, "SELECT subject_type, subject_id, kind, notes FROM data_requests WHERE business_id = :b",
                    b=shop.bid)  # fmt: skip
    assert (row.subject_type, row.subject_id, str(row.kind), row.notes) == (
        "customer",
        customer.id,
        "deletion",
        "wants it all gone",
    )


@pytest.mark.check("CHK-UI-5")
async def test_owner_routes_see_only_their_own_shop_and_operator_routes_need_an_operator(
    registry: Registry, db: Database, client: httpx.AsyncClient, setting: Any
) -> None:
    mine, theirs = await bakery(db), await bakery(db)
    token, _session = auth.issue("Meera", business_id=str(mine.bid))
    client.cookies.set(auth.OWNER_COOKIE, token)
    headers = {auth.SURFACE_HEADER: "app"}
    own = await client.get("/admin/api/conversations", params={"business_id": str(mine.bid)}, headers=headers)
    assert own.status_code == 200
    other = await client.get("/admin/api/conversations", params={"business_id": str(theirs.bid)}, headers=headers)
    assert other.status_code == 404  # another shop does not exist for this owner
    assert (await client.get("/admin/api/health", headers=headers)).status_code == 403  # operator only
    client.cookies.clear()

    setting(operator_totp_required="true", operator_totp_secret=TOTP_SECRET)  # an operator session needs the code
    auth._attempts.clear()
    creds = {"username": "admin", "password": "test-admin-password"}
    assert (await client.post("/admin/api/login", json=creds)).status_code == 401
    assert (await client.get("/admin/api/health")).status_code == 401
    assert (await client.post("/admin/api/login", json=creds | {"totp": auth.totp(TOTP_SECRET)})).status_code == 200
    assert (await client.get("/admin/api/health")).status_code == 200
    auth._attempts.clear()


async def test_the_status_page_follows_the_request(registry: Registry, db: Database, client: httpx.AsyncClient) -> None:
    from reflexo.workflows import privacy

    shop = await bakery(db)
    await _connected_by(db, shop.bid, "fb-user-999")
    body = (await client.post("/meta/data-deletion", data={"signed_request": signed(
        {"algorithm": "HMAC-SHA256", "user_id": "fb-user-999"})})).json()  # fmt: skip
    code = body["confirmation_code"]
    for status, shown in (("in_progress", "in_progress"), ("verifying", "received"), ("completed", "completed")):
        async with db.service_session(ServiceReason.ADMIN) as s:
            await s.execute(text("UPDATE data_requests SET status = CAST(:s AS data_request_status) WHERE id = :i"),
                            {"s": status, "i": code})  # fmt: skip
        assert await privacy.deletion_status(code) == shown
    assert await privacy.deletion_status("not-a-code") == "unknown"
    assert await privacy.deletion_status("00000000-0000-0000-0000-000000000000") == "unknown"
