"""Gateway end-to-end on a real database: webhook → inbox → normalize → identity → message → turn; outbound
policy/render/enqueue/send; echo takeover; opt-out; verified linking; status updates (POC_03 §2–§10)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

import reflexo.admin.api as admin
import reflexo.gateway.api as gateway_api
from reflexo.api.main import app
from reflexo.common.contracts import InboundProcessorPort, InboxEvent, OutboundRow, SendStatus
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.common.timeutil import utcnow
from reflexo.gateway import identity
from reflexo.gateway.adapters import simulator
from reflexo.gateway.adapters.meta_common import sign
from reflexo.pipeline import api as pipeline_api
from tests.factories import vault_credential

pytestmark = pytest.mark.integration

APP_SECRET = "test_meta_app_secret"
OWNER_PHONE = "+919800000001"
LAB_OWNER_PHONE = "+919800000002"


@dataclass
class Shop:
    business_id: UUID
    wa: UUID
    tg: UUID
    fb: UUID
    sim: UUID
    pnid: str
    page_id: str
    owner_id: UUID


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    return reg


async def make_shop(db: Database) -> Shop:
    bid, owner = new_id(), new_id()
    wa, tg, fb, sim = new_id(), new_id(), new_id(), new_id()
    pnid, page_id = f"PN{bid.hex[-12:]}", f"PG{bid.hex[-12:]}"
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO businesses (id, name, slug, status) VALUES (:id, 'Shop', :slug, 'active')"),
            {"id": bid, "slug": f"shop-{bid}"},
        )
        for cid, channel, provider, ext, cred in (
            (wa, "whatsapp", "meta_whatsapp", pnid, "REFLEXO_WA"),
            (tg, "telegram", "telegram", f"tg-{bid.hex[-8:]}", "REFLEXO_TELEGRAM"),
            (fb, "facebook_messenger", "meta_pages", page_id, "REFLEXO_FB"),
            (sim, "web_widget", "simulator", f"sim-{bid.hex[-8:]}", None),
        ):
            await s.execute(
                text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                                         display_name, credential_id, roles)
                        VALUES (:id, :b, CAST(:ch AS channel_type), CAST(:p AS channel_provider), :e, :ch, :cred,
                                '{customer,owner}')"""),
                {
                    "id": cid,
                    "b": bid,
                    "ch": channel,
                    "p": provider,
                    "e": ext,
                    "cred": await vault_credential(s, bid, provider, cred) if cred else None,
                },
            )
        await s.execute(
            text("INSERT INTO team_members (id, business_id, display_name, role) VALUES (:id, :b, 'Meera', 'owner')"),
            {"id": owner, "b": bid},
        )
        for phone, sim_only in ((OWNER_PHONE, False), (LAB_OWNER_PHONE, True)):
            await s.execute(
                text("""INSERT INTO owner_channel_bindings (id, business_id, member_id, channel, external_id,
                                                            verified_at, simulated_only)
                        VALUES (:id, :b, :m, 'whatsapp', :e, now(), :so)"""),
                {"id": new_id(), "b": bid, "m": owner, "e": phone, "so": sim_only},
            )
    return Shop(bid, wa, tg, fb, sim, pnid, page_id, owner)


# ---------------------------------------------------------------- payload builders


def wa_message(pnid: str, wa_id: str, mid: str, body: str | None = None, **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"from": wa_id, "id": mid, "timestamp": "1760000000"}
    msg |= extra or {"type": "text", "text": {"body": body}}
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": pnid},
        "contacts": [{"profile": {"name": "Rahul"}, "wa_id": wa_id}],
        "messages": [msg],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def wa_status(pnid: str, provider_id: str, status: str) -> dict[str, Any]:
    st: dict[str, Any] = {"id": provider_id, "status": status, "timestamp": "1760000001", "recipient_id": "1"}
    if status == "failed":
        st["errors"] = [{"code": 131026, "title": "Undeliverable"}]
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid}, "statuses": [st]}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def fb_message(page_id: str, psid: str, mid: str, body: str, *, echo_app: int | None = None) -> dict[str, Any]:
    if echo_app is not None:
        item = {
            "sender": {"id": page_id},
            "recipient": {"id": psid},
            "timestamp": 1760000000000,
            "message": {"mid": mid, "is_echo": True, "app_id": echo_app, "text": body},
        }
    else:
        item = {
            "sender": {"id": psid},
            "recipient": {"id": page_id},
            "timestamp": 1760000000000,
            "message": {"mid": mid, "text": body},
        }
    return {"object": "page", "entry": [{"id": page_id, "messaging": [item]}]}


# ---------------------------------------------------------------- helpers


async def post(
    provider: str, payload: dict[str, Any], *, path: str = "", simulated: bool = False, bad_sig: bool = False
) -> httpx.Response:
    raw = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if provider.startswith("meta_"):
        headers["x-hub-signature-256"] = sign(raw, "wrong" if bad_sig else APP_SECRET)
    elif provider == "telegram":
        headers["x-telegram-bot-api-secret-token"] = "wrong" if bad_sig else "test_tg_secret"
    elif provider == "simulator":
        headers["x-reflexo-signature"] = simulator.sign(b"x" if bad_sig else raw)
    if simulated:
        headers["x-reflexo-simulated"] = "1"
    url = f"/webhooks/{provider}" + (f"/{path}" if path else "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(url, content=raw, headers=headers)


async def inbox_rows(db: Database, route_key: str) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list(
            (
                await s.execute(
                    text("SELECT * FROM inbox_events WHERE route_key = :r ORDER BY received_at, id"), {"r": route_key}
                )
            ).all()
        )


async def process_pending(registry: Registry, db: Database, route_key: str) -> None:
    """Run the inbound processor for every inbox row of this route that has not been processed by this test."""
    processor = registry.require(InboundProcessorPort)  # type: ignore[type-abstract]
    for row in await inbox_rows(db, route_key):
        if row.status != "pending":
            continue
        await processor.process_inbox_event(
            InboxEvent(
                row.id,
                str(row.provider),
                row.route_key,
                row.dedupe_key,
                row.headers,
                row.payload,
                row.simulated,
                row.attempts,
                row.trace_context,
            )
        )
        async with db.service_session(ServiceReason.ADMIN) as s:
            await s.execute(text("UPDATE inbox_events SET status = 'done' WHERE id = :id"), {"id": row.id})


async def deliver(
    registry: Registry,
    db: Database,
    provider: str,
    payload: dict[str, Any],
    route_key: str,
    *,
    path: str = "",
    simulated: bool = False,
) -> None:
    resp = await post(provider, payload, path=path, simulated=simulated)
    assert resp.status_code == 200, resp.text
    await process_pending(registry, db, route_key)


async def q(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), params)).all())


async def conversation_for(db: Database, connection_id: UUID, thread_key: str) -> Any:
    [row] = await q(
        db,
        "SELECT * FROM conversations WHERE channel_connection_id = :c AND thread_key = :t",
        c=connection_id,
        t=thread_key,
    )
    return row


# ---------------------------------------------------------------- ingress


async def test_meta_verify_challenge() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(
            "/webhooks/meta_whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "test_verify_token", "hub.challenge": "12345"},
        )
        bad = await client.get(
            "/webhooks/meta_pages",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"},
        )
    assert (ok.status_code, ok.text) == (200, "12345")
    assert bad.status_code == 403


async def test_bad_signature_is_401_and_not_stored(db: Database) -> None:
    shop = await make_shop(db)
    for provider, payload, path in (
        ("meta_whatsapp", wa_message(shop.pnid, "919811111111", "wamid.BAD", "hi"), ""),
        ("telegram", {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1}, "text": "x"}}, str(shop.tg)),
        ("simulator", {"sender": {"id": "u1"}, "text": "x"}, str(shop.sim)),
    ):
        resp = await post(provider, payload, path=path, bad_sig=True)
        assert resp.status_code == 401, provider
    assert await inbox_rows(db, shop.pnid) == []
    assert await inbox_rows(db, str(shop.tg)) == []


async def test_oversized_payload_rejected(db: Database) -> None:
    resp = await post("meta_whatsapp", {"object": "x", "pad": "a" * 2_000_000})
    assert resp.status_code == 413


async def test_duplicate_webhook_stored_once(db: Database) -> None:
    shop = await make_shop(db)
    payload = wa_message(shop.pnid, "919811111112", "wamid.DUP", "hi")
    for _ in range(3):
        assert (await post("meta_whatsapp", payload)).status_code == 200
    assert len(await inbox_rows(db, shop.pnid)) == 1


async def test_telegram_update_ids_are_scoped_per_bot(db: Database) -> None:
    a, b = await make_shop(db), await make_shop(db)
    update = {
        "update_id": 777,
        "message": {
            "message_id": 1,
            "date": 1760000000,
            "chat": {"id": 42},
            "from": {"id": 42, "first_name": "R"},
            "text": "hi",
        },
    }
    for shop in (a, b):
        assert (await post("telegram", update, path=str(shop.tg))).status_code == 200
    assert len(await inbox_rows(db, str(a.tg))) == 1
    assert len(await inbox_rows(db, str(b.tg))) == 1


# ---------------------------------------------------------------- inbound processing


async def test_whatsapp_text_end_to_end(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    payload = wa_message(shop.pnid, "919811111113", "wamid.E2E", "Do you have eggless chocolate cake?")
    await deliver(registry, db, "meta_whatsapp", payload, shop.pnid)

    conv = await conversation_for(db, shop.wa, "919811111113")
    assert (str(conv.participant_role), conv.simulated) == ("customer", False)
    assert conv.last_inbound_at is not None
    [cust] = await q(db, "SELECT * FROM customers WHERE id = :id", id=conv.customer_id)
    assert (cust.phone_e164, cust.display_name) == ("+919811111113", "Rahul")
    [msg] = await q(db, "SELECT * FROM messages WHERE conversation_id = :c", c=conv.id)
    assert (str(msg.direction), str(msg.author), msg.content_text) == (
        "inbound",
        "customer",
        "Do you have eggless chocolate cake?",
    )
    [event] = await q(db, "SELECT * FROM domain_events WHERE conversation_id = :c", c=conv.id)
    assert event.type == "message.received" and event.payload["message_id"] == str(msg.id)
    [turn] = await q(db, "SELECT * FROM conversation_turns WHERE conversation_id = :c", c=conv.id)
    assert str(turn.desk) == "customer"
    jobs = await q(db, "SELECT queue FROM jobs WHERE business_id = :b", b=shop.business_id)
    assert [j.queue for j in jobs] == ["gateway.typing"]

    # the provider re-sends the same message with a different envelope (new inbox row): still one message
    payload["entry"][0]["id"] = "WABA-redelivery"
    await deliver(registry, db, "meta_whatsapp", payload, shop.pnid)
    assert len(await q(db, "SELECT id FROM messages WHERE conversation_id = :c", c=conv.id)) == 1


async def test_image_enqueues_media_fetch(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    payload = wa_message(
        shop.pnid,
        "919811111114",
        "wamid.IMG",
        type="image",
        image={"id": "media_9", "mime_type": "image/jpeg", "caption": "this design"},
    )
    await deliver(registry, db, "meta_whatsapp", payload, shop.pnid)
    [job] = await q(db, "SELECT * FROM jobs WHERE business_id = :b AND queue = 'media.fetch'", b=shop.business_id)
    assert job.payload["media"]["provider_media_id"] == "media_9"
    assert job.payload["owner_member_id"] is None


async def test_unknown_account_is_ignored(registry: Registry, db: Database) -> None:
    payload = wa_message("PN-unknown", "919811111115", "wamid.UNK", "hi")
    await deliver(registry, db, "meta_whatsapp", payload, "PN-unknown")
    assert await q(db, "SELECT id FROM messages WHERE external_message_id = 'wamid.UNK'") == []


async def test_verified_owner_goes_to_owner_desk(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(shop.pnid, OWNER_PHONE[1:], "wamid.OWN", "how many orders today?"),
        shop.pnid,
    )
    conv = await conversation_for(db, shop.wa, OWNER_PHONE[1:])
    assert str(conv.participant_role) == "owner" and conv.member_id == shop.owner_id and conv.customer_id is None
    [turn] = await q(db, "SELECT desk, priority FROM conversation_turns WHERE conversation_id = :c", c=conv.id)
    assert (str(turn.desk), str(turn.priority)) == ("owner", "p1")
    assert await q(db, "SELECT id FROM domain_events WHERE conversation_id = :c", c=conv.id) == []


async def test_simulated_only_binding_needs_signed_simulated_event(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    wa_id = LAB_OWNER_PHONE[1:]
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, wa_id, "wamid.LAB1", "hi"), shop.pnid)
    real = await conversation_for(db, shop.wa, wa_id)
    assert str(real.participant_role) == "customer"  # a real sender cannot use the Lab binding

    other = await make_shop(db)
    await deliver(
        registry, db, "meta_whatsapp", wa_message(other.pnid, wa_id, "wamid.LAB2", "hi"), other.pnid, simulated=True
    )
    lab = await conversation_for(db, other.wa, wa_id)
    assert (str(lab.participant_role), lab.simulated) == ("owner", True)


async def test_simulator_channel_end_to_end(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    payload = {"event_id": f"ev-{new_id()}", "sender": {"id": "lab-user-1", "name": "Priya"}, "text": "menu please"}
    await deliver(registry, db, "simulator", payload, str(shop.sim), path=str(shop.sim))
    conv = await conversation_for(db, shop.sim, "lab-user-1")
    assert len(await q(db, "SELECT id FROM conversation_turns WHERE conversation_id = :c", c=conv.id)) == 1
    assert (
        await q(db, "SELECT id FROM jobs WHERE business_id = :b AND queue = 'gateway.typing'", b=shop.business_id) == []
    )


async def test_reply_text_maps_to_offered_option(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, "919811111116", "wamid.O1", "hi"), shop.pnid)
    conv = await conversation_for(db, shop.wa, "919811111116")
    async with db.tenant_session(shop.business_id) as s:
        await gateway_api.outbound().enqueue(
            s,
            business_id=shop.business_id,
            conversation_id=conv.id,
            origin="ai_turn",
            idempotency_base=f"t:{new_id()}",
            blocks=[
                {
                    "type": "list",
                    "text": "Which size?",
                    "options": [
                        {"id": f"s{i}", "title": t} for i, t in enumerate(["Half kg", "One kg", "Two kg", "Three kg"])
                    ],
                }
            ],
        )
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, "919811111116", "wamid.O2", "do"), shop.pnid)
    [msg] = await q(db, "SELECT interactive_reply FROM messages WHERE external_message_id = 'wamid.O2'")
    assert msg.interactive_reply["option_id"] == "s1"


# ---------------------------------------------------------------- echo → human takeover


async def test_page_inbox_echo_starts_human_takeover(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    await deliver(registry, db, "meta_pages", fb_message(shop.page_id, "PSID9", "m.1", "hi"), shop.page_id)
    conv = await conversation_for(db, shop.fb, "PSID9")

    # our own API send echoes back with our app id: ignored
    await deliver(
        registry, db, "meta_pages", fb_message(shop.page_id, "PSID9", "m.2", "bot", echo_app=111222333), shop.page_id
    )
    assert (await conversation_for(db, shop.fb, "PSID9")).human_active_until is None

    # Meera replies from the Page inbox: takeover
    await deliver(
        registry,
        db,
        "meta_pages",
        fb_message(shop.page_id, "PSID9", "m.3", "Meera here!", echo_app=263902037430900),
        shop.page_id,
    )
    taken = await conversation_for(db, shop.fb, "PSID9")
    assert taken.human_active_until is not None and taken.human_active_until > utcnow() + timedelta(minutes=25)
    [team] = await q(db, "SELECT content_text FROM messages WHERE conversation_id = :c AND author = 'team'", c=conv.id)
    assert team.content_text == "Meera here!"

    await deliver(registry, db, "meta_pages", fb_message(shop.page_id, "PSID9", "m.4", "ok thanks"), shop.page_id)
    skipped = await q(
        db,
        """SELECT attrs FROM pipeline_events WHERE conversation_id = :c AND stage = 'turn_scheduled'
                             AND status = 'skip'""",
        c=conv.id,
    )
    assert skipped and skipped[-1].attrs["reason"] == "human"  # a person holds the chat (not "AI off")


# ---------------------------------------------------------------- opt-out / opt-in


async def test_opt_out_blocks_ai_and_opt_in_restores(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    wa_id = "919811111117"
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, wa_id, "wamid.S1", "STOP"), shop.pnid)
    conv = await conversation_for(db, shop.wa, wa_id)
    assert await q(db, "SELECT id FROM conversation_turns WHERE conversation_id = :c", c=conv.id) == []
    [confirm] = await q(
        db,
        "SELECT rendered_payload FROM outbound_messages WHERE idempotency_key LIKE 'opt:%' AND conversation_id = :c",
        c=conv.id,
    )
    assert "START" in confirm.rendered_payload["text"]["body"]

    async with db.tenant_session(shop.business_id) as s:
        result = await gateway_api.outbound().enqueue(
            s,
            business_id=shop.business_id,
            conversation_id=conv.id,
            origin="ai_turn",
            idempotency_base=f"t:{new_id()}",
            blocks=[{"type": "text", "text": "Diwali offer!"}],
        )
    assert (result.status, result.code) == ("blocked", "OPTED_OUT")
    [blocked] = await q(db, "SELECT status, error FROM messages WHERE id = :id", id=result.message_id)
    assert str(blocked.status) == "blocked" and blocked.error["code"] == "OPTED_OUT"

    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, wa_id, "wamid.S2", "start"), shop.pnid)
    async with db.tenant_session(shop.business_id) as s:
        again = await gateway_api.outbound().enqueue(
            s,
            business_id=shop.business_id,
            conversation_id=conv.id,
            origin="ai_turn",
            idempotency_base=f"t:{new_id()}",
            blocks=[{"type": "text", "text": "Welcome back"}],
        )
    assert again.status == "queued"


# ---------------------------------------------------------------- verified cross-channel linking


async def test_link_code_merges_telegram_customer_into_whatsapp(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    tg_update = {
        "update_id": 501,
        "message": {
            "message_id": 1,
            "date": 1760000000,
            "chat": {"id": 8080},
            "from": {"id": 8080, "first_name": "Rahul"},
            "text": "hi",
        },
    }
    await deliver(registry, db, "telegram", tg_update, str(shop.tg), path=str(shop.tg))
    tg_conv = await conversation_for(db, shop.tg, "8080")
    async with db.tenant_session(shop.business_id) as s:
        code, url = await gateway_api.issue_link_code(s, shop.business_id, tg_conv.customer_id, "telegram")
    assert url == f"https://wa.me/919800000000?text={code}"

    wa_id = "919811111118"
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, wa_id, "wamid.L1", code.lower()), shop.pnid)
    wa_conv = await conversation_for(db, shop.wa, wa_id)
    assert wa_conv.customer_id == tg_conv.customer_id  # conversation moved to the surviving customer
    [ident] = await q(
        db,
        "SELECT customer_id, linked_via FROM identity_map WHERE business_id = :b AND "
        "channel = 'whatsapp' AND external_id = :e",
        b=shop.business_id,
        e="+" + wa_id,
    )
    assert (ident.customer_id, ident.linked_via) == (tg_conv.customer_id, "link_code")
    merged = await q(
        db,
        "SELECT id FROM customers WHERE business_id = :b AND merged_into = :k",
        b=shop.business_id,
        k=tg_conv.customer_id,
    )
    assert len(merged) == 1
    [kept] = await q(db, "SELECT phone_e164 FROM customers WHERE id = :id", id=tg_conv.customer_id)
    assert kept.phone_e164 == "+" + wa_id  # verified by WhatsApp possession
    assert await q(db, "SELECT id FROM conversation_turns WHERE conversation_id = :c", c=wa_conv.id) == []

    # a code works once
    async with db.tenant_session(shop.business_id) as s:
        again = await identity.consume_link_code(s, shop.business_id, code, tg_conv.customer_id)
    assert again.status == "expired"

    # admin (CHK-F13-1): one customer, both identities, one history across channels
    [listed] = await admin.workspace.customers(shop.business_id)
    assert listed["merged"] == 1 and {i["channel"] for i in listed["identities"]} == {"telegram", "whatsapp"}
    view = await admin.workspace.customer_detail(shop.business_id, tg_conv.customer_id)
    assert view is not None
    assert {(i["channel"], i["linked_via"]) for i in view["identities"]} == {
        ("telegram", "first_contact"),
        ("whatsapp", "link_code"),
    }
    assert [(h["channel"], h["text"]) for h in view["history"] if h["direction"] == "inbound"] == [
        ("telegram", "hi"),
        ("whatsapp", code.lower()),
    ]


async def test_expired_and_unknown_link_codes(db: Database) -> None:
    shop = await make_shop(db)
    async with db.tenant_session(shop.business_id) as s:
        a, _ = await identity.resolve_customer(s, shop.business_id, "telegram", "tg-1", None, None)
        b, _ = await identity.resolve_customer(s, shop.business_id, "whatsapp", "+919811111119", None, None)
        code, _ = await identity.issue_link_code(s, shop.business_id, a, "telegram")
        await s.execute(
            text("UPDATE customer_link_codes SET expires_at = now() - interval '1 minute' WHERE code = :c"), {"c": code}
        )
        assert (await identity.consume_link_code(s, shop.business_id, code, b)).status == "expired"
        assert (await identity.consume_link_code(s, shop.business_id, "LINK-ZZZZZ", b)).status == "unknown"
    [row] = await q(db, "SELECT status FROM customer_link_codes WHERE code = :c", c=code)
    assert str(row.status) == "expired"


async def test_concurrent_first_messages_create_one_customer(db: Database) -> None:
    shop = await make_shop(db)

    async def resolve() -> UUID:
        async with db.service_session(ServiceReason.INBOUND_RESOLVE) as s:
            cid, _ = await identity.resolve_customer(s, shop.business_id, "whatsapp", "+919811111120", "R", shop.wa)
            await asyncio.sleep(0.05)  # keep transactions overlapping
            return cid

    ids = await asyncio.gather(*(resolve() for _ in range(5)))
    assert len(set(ids)) == 1
    rows = await q(db, "SELECT id FROM customers WHERE business_id = :b", b=shop.business_id)
    assert [r.id for r in rows] == [ids[0]]


# ---------------------------------------------------------------- outbound enqueue / send / status


async def _wa_conv(registry: Registry, db: Database, shop: Shop, wa_id: str, simulated: bool = False) -> Any:
    await deliver(
        registry,
        db,
        "meta_whatsapp",
        wa_message(shop.pnid, wa_id, f"wamid.{new_id()}", "hi"),
        shop.pnid,
        simulated=simulated,
    )
    return await conversation_for(db, shop.wa, wa_id)


async def _enqueue(
    db: Database,
    shop: Shop,
    conv_id: UUID,
    blocks: list[dict[str, Any]],
    *,
    origin: str = "ai_turn",
    key: str | None = None,
) -> gateway_api.EnqueueResult:
    async with db.tenant_session(shop.business_id) as s:
        return await gateway_api.outbound().enqueue(
            s,
            business_id=shop.business_id,
            conversation_id=conv_id,
            blocks=blocks,
            origin=origin,
            idempotency_base=key or f"t:{new_id()}",
        )


async def test_enqueue_renders_whatsapp_parts_idempotently(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    conv = await _wa_conv(registry, db, shop, "919811111121")
    blocks = [
        {"type": "text", "text": "We have 3 sizes:"},
        {
            "type": "buttons",
            "text": "Pick one",
            "options": [{"id": "a", "title": "Half kg"}, {"id": "b", "title": "One kg"}],
        },
    ]
    first = await _enqueue(db, shop, conv.id, blocks, key="turn-x")
    again = await _enqueue(db, shop, conv.id, blocks, key="turn-x")
    assert first.status == "queued" and len(first.part_ids) == 2
    assert (again.status, again.message_id) == ("duplicate", first.message_id)
    parts = await q(
        db,
        "SELECT part_index, rendered_payload FROM outbound_messages WHERE message_id = :m ORDER BY part_index",
        m=first.message_id,
    )
    assert parts[0].rendered_payload["text"]["body"] == "We have 3 sizes:"
    assert parts[1].rendered_payload["interactive"]["type"] == "button"
    assert parts[1].rendered_payload["to"] == "919811111121"
    [c] = await q(db, "SELECT option_map FROM conversations WHERE id = :id", id=conv.id)
    assert [o["option_id"] for o in c.option_map["options"]] == ["a", "b"]


async def test_window_closed_uses_template_and_human_active_blocks(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    conv = await _wa_conv(registry, db, shop, "919811111122")
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE conversations SET last_inbound_at = now() - interval '25 hours' WHERE id = :c"), {"c": conv.id}
        )
    tpl = await _enqueue(db, shop, conv.id, [{"type": "text", "text": "Your cake is ready!"}], origin="automation")
    [p] = await q(db, "SELECT rendered_payload FROM outbound_messages WHERE message_id = :m", m=tpl.message_id)
    assert p.rendered_payload["type"] == "template"
    assert p.rendered_payload["template"]["components"][0]["parameters"][0]["text"] == "Your cake is ready!"

    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""UPDATE conversations SET last_inbound_at = now(),
                                human_active_until = now() + interval '10 minutes' WHERE id = :c"""),
            {"c": conv.id},
        )
    blocked = await _enqueue(db, shop, conv.id, [{"type": "text", "text": "AI reply"}])
    assert (blocked.status, blocked.code) == ("blocked", "HUMAN_ACTIVE")
    owner_reply = await _enqueue(db, shop, conv.id, [{"type": "text", "text": "Meera here"}], origin="team_reply")
    assert owner_reply.status == "queued"  # humans are never blocked by takeover


async def test_simulated_conversation_sends_through_lab_transport(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    conv = await _wa_conv(registry, db, shop, "919899999901", simulated=True)
    assert conv.simulated
    result = await _enqueue(db, shop, conv.id, [{"type": "text", "text": "Hello from the Lab"}])
    [row] = await q(db, "SELECT * FROM outbound_messages WHERE message_id = :m", m=result.message_id)
    send = await gateway_api.outbound().send(
        OutboundRow(
            row.id,
            row.business_id,
            row.message_id,
            row.conversation_id,
            row.channel_connection_id,
            row.part_index,
            row.idempotency_key,
            row.rendered_payload,
            row.attempts,
            str(row.status),
            row.provider_message_id,
            row.trace_context,
        )
    )
    assert send.status == SendStatus.SENT and send.provider_message_id is not None
    assert send.provider_message_id.startswith("sim-")
    [ev] = await q(
        db, "SELECT attrs FROM pipeline_events WHERE stage = 'sim_message' AND conversation_id = :c", c=conv.id
    )
    assert ev.attrs["payload"]["messaging_product"] == "whatsapp"  # the real WhatsApp payload, not a lab format
    assert ev.attrs["provider"] == "meta_whatsapp"


async def test_status_webhooks_move_forward_only(registry: Registry, db: Database) -> None:
    shop = await make_shop(db)
    conv = await _wa_conv(registry, db, shop, "919811111123")
    result = await _enqueue(db, shop, conv.id, [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    pids = ["wamid.P0", "wamid.P1"]
    async with db.service_session(ServiceReason.ADMIN) as s:
        for i, pid in enumerate(pids):
            await s.execute(
                text("""UPDATE outbound_messages SET status = 'sent', provider_message_id = :p
                                    WHERE message_id = :m AND part_index = :i"""),
                {"p": pid, "m": result.message_id, "i": i},
            )
        await s.execute(
            text("UPDATE messages SET status = 'sent', external_message_id = 'wamid.P0' WHERE id = :m"),
            {"m": result.message_id},
        )

    async def status_now() -> str:
        [m] = await q(db, "SELECT status FROM messages WHERE id = :m", m=result.message_id)
        return str(m.status)

    await deliver(registry, db, "meta_whatsapp", wa_status(shop.pnid, "wamid.P1", "read"), shop.pnid)
    assert await status_now() == "read"  # matched through the second part's provider id
    await deliver(registry, db, "meta_whatsapp", wa_status(shop.pnid, "wamid.P0", "delivered"), shop.pnid)
    assert await status_now() == "read"  # a late "delivered" does not move it backwards
    await deliver(registry, db, "meta_whatsapp", wa_status(shop.pnid, "wamid.P0", "failed"), shop.pnid)
    assert await status_now() == "failed"
    [part] = await q(db, "SELECT status FROM outbound_messages WHERE provider_message_id = 'wamid.P0'")
    assert str(part.status) == "failed"


async def test_nameless_customer_gets_profile_name_later(registry: Registry, db: Database) -> None:
    """Live finding: a customer first created by a call signal had no name, so 'Taha ko bolo…' found nobody."""
    shop = await make_shop(db)
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, "919811111177", "wamid.N1", "hi"), shop.pnid)
    conv = await conversation_for(db, shop.wa, "919811111177")
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("UPDATE customers SET display_name = NULL WHERE id = :c"), {"c": conv.customer_id})
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, "919811111177", "wamid.N2", "hello"), shop.pnid)
    [cust] = await q(db, "SELECT display_name FROM customers WHERE id = :id", id=conv.customer_id)
    assert cust.display_name == "Rahul"
    async with db.service_session(ServiceReason.ADMIN) as s:  # a name set by the shop is never overwritten
        await s.execute(
            text("UPDATE customers SET display_name = 'Rahul Verma' WHERE id = :c"), {"c": conv.customer_id}
        )
    await deliver(registry, db, "meta_whatsapp", wa_message(shop.pnid, "919811111177", "wamid.N3", "ok"), shop.pnid)
    [cust] = await q(db, "SELECT display_name FROM customers WHERE id = :id", id=conv.customer_id)
    assert cust.display_name == "Rahul Verma"
