"""Simulated actors (POC_07 §3.2): provider-shaped, signed payloads into the REAL ingress.

A simulated customer is a fake wa_id on the business's WhatsApp number; the event is flagged simulated, so the
whole pipeline runs and only the final provider call is swapped for the Lab transport (replies never reach Meta).
"Pay" posts a correctly signed Razorpay `payment_link.paid` webhook.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
import reflexo.integrations.api as integrations
from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id

SIM_PREFIX = "91999900"  # fake wa_ids: 9199990000 + actor number


def actor_wa_id(actor: int) -> str:
    return f"{SIM_PREFIX}{actor:04d}"


@dataclass(frozen=True)
class SimMedia:
    """A file the simulated phone sent: stored first (`gateway.store_simulated_upload`), then referenced."""

    media_object_id: UUID
    mime: str
    filename: str | None = None


def _message(
    mid: str, text_: str | None, option_id: str | None, option_title: str | None, media: SimMedia | None
) -> dict[str, Any]:
    if option_id:
        return {
            "id": mid,
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": option_id, "title": option_title or ""}},
        }
    if media is not None:
        kind = "image" if media.mime.startswith("image/") else "document"
        body: dict[str, Any] = {"id": f"{gateway.SIM_MEDIA_PREFIX}{media.media_object_id}", "mime_type": media.mime}
        if text_:
            body["caption"] = text_
        if kind == "document":
            body["filename"] = media.filename or "file"
        return {"id": mid, "type": kind, kind: body}
    return {"id": mid, "type": "text", "text": {"body": text_ or ""}}


@dataclass(frozen=True)
class SimResult:
    status: int
    external_message_id: str
    wa_id: str


async def whatsapp_number(business_id: UUID) -> str:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        pnid: str | None = (
            await s.execute(
                text("""SELECT external_account_id FROM channel_connections
                        WHERE business_id = :b AND channel = 'whatsapp' AND status = 'active' LIMIT 1"""),
                {"b": business_id},
            )
        ).scalar_one_or_none()
    if pnid is None:
        raise LookupError("this business has no WhatsApp connection")
    return pnid


def _payload(pnid: str, wa_id: str, name: str, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-SIM",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": pnid},
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [{"from": wa_id, "timestamp": str(int(time.time())), **message}],
                        },
                    }
                ],
            }
        ],
    }


async def customer_says(
    business_id: UUID,
    actor: int,
    *,
    text_: str | None = None,
    option_id: str | None = None,
    option_title: str | None = None,
    external_message_id: str | None = None,
    name: str | None = None,
    media: SimMedia | None = None,
) -> SimResult:
    """One simulated customer message (text, photo/file, or button tap) through the signed WhatsApp ingress."""
    pnid = await whatsapp_number(business_id)
    wa_id = actor_wa_id(actor)
    mid = external_message_id or f"wamid.SIM{new_id().hex}"
    message = _message(mid, text_, option_id, option_title, media)
    raw = json.dumps(_payload(pnid, wa_id, name or f"Sim Customer {actor}", message)).encode()
    return SimResult(await post_raw(raw), mid, wa_id)


async def post_raw(raw: bytes) -> int:
    """Post exact bytes (a duplicate webhook is the same bytes again) with a valid signature, flagged simulated."""
    result = await gateway.ingest_webhook(
        "meta_whatsapp",
        {"x-hub-signature-256": gateway.sign_meta(raw), "x-reflexo-simulated": "1", "content-type": "application/json"},
        raw,
        {},
    )
    return result.status_code


async def customer_message_bytes(business_id: UUID, actor: int, text_: str, external_message_id: str) -> bytes:
    pnid = await whatsapp_number(business_id)
    wa_id = actor_wa_id(actor)
    message = {"id": external_message_id, "type": "text", "text": {"body": text_}}
    return json.dumps(_payload(pnid, wa_id, f"Sim Customer {actor}", message)).encode()


async def pay_link(business_id: UUID, payment_link_id: UUID) -> int:
    """Signed Razorpay test webhook for one of our payment links (the Lab's "Pay" button)."""
    async with get_db().tenant_session(business_id) as s:
        link = (
            await s.execute(
                text("SELECT id, provider_link_id, amount_paise FROM payment_links WHERE id = :id"),
                {"id": payment_link_id},
            )
        ).one()
    body = {
        "event": "payment_link.paid",
        "created_at": int(time.time()),
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link.provider_link_id,
                    "reference_id": str(link.id),
                    "status": "paid",
                    "amount_paid": link.amount_paise,
                }
            },
            "payment": {
                "entity": {"id": f"pay_SIM{new_id().hex[:14]}", "amount": link.amount_paise, "status": "captured"}
            },
        },
    }
    raw = json.dumps(body).encode()
    secret = get_settings().razorpay_webhook_secret.get_secret_value()
    headers = {"x-razorpay-signature": integrations.sign(raw, secret), "x-razorpay-event-id": f"evt_SIM{new_id().hex}"}
    return (await integrations.ingest_razorpay(headers, raw)).status_code


async def thread(business_id: UUID, actor: int, limit: int = 50) -> dict[str, Any]:
    """What the simulated customer's phone shows: their messages and the rendered replies (provider payloads)."""
    return await thread_by_key(business_id, actor_wa_id(actor), limit)


async def _items(business_id: UUID, conv: UUID, limit: int) -> dict[str, Any]:
    async with get_db().tenant_session(business_id) as s:
        inbound = (
            await s.execute(
                text("""SELECT created_at, content_text, interactive_reply, attachments FROM messages
                        WHERE conversation_id = :c AND direction = 'inbound' ORDER BY created_at DESC LIMIT :n"""),
                {"c": conv, "n": limit},
            )
        ).all()
        outbound = (
            await s.execute(
                text("""SELECT created_at, rendered_payload, status FROM outbound_messages
                        WHERE conversation_id = :c ORDER BY created_at DESC, part_index LIMIT :n"""),
                {"c": conv, "n": limit},
            )
        ).all()
        links = (
            await s.execute(
                text("""SELECT p.id, p.amount_paise, p.status, p.short_url, o.order_number FROM payment_links p
                        JOIN orders o ON o.id = p.order_id WHERE o.conversation_id = :c
                        ORDER BY p.created_at DESC LIMIT 5"""),
                {"c": conv},
            )
        ).all()
    signer = gateway.media_signer()
    items = [
        {
            "at": r.created_at.isoformat(),
            "from": "customer",
            "text": r.content_text,
            "option": (r.interactive_reply or {}).get("title"),
            "media": [
                {
                    "url": signer.url_for(UUID(str(a["media_object_id"]))),
                    "kind": a.get("kind"),
                    "filename": a.get("filename"),
                }
                for a in (r.attachments or [])
                if a.get("media_object_id")
            ],
        }
        for r in inbound
    ] + [
        {"at": r.created_at.isoformat(), "from": "business", "payload": r.rendered_payload, "status": str(r.status)}
        for r in outbound
    ]
    items.sort(key=lambda i: str(i["at"]))
    return {
        "conversation_id": str(conv),
        "items": items[-limit:],
        "payment_links": [
            {
                "id": str(r.id),
                "amount_paise": r.amount_paise,
                "status": str(r.status),
                "url": r.short_url,
                "order_number": r.order_number,
            }
            for r in links
        ],
    }


# ---------------------------------------------------------------- the simulated owner (POC_07 §3.2)


async def sim_owner_wa_id(business_id: UUID) -> str | None:
    """The business's simulated owner number (an `owner_channel_bindings` row flagged `simulated_only`)."""
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        ext: str | None = (
            await s.execute(
                text("""SELECT external_id FROM owner_channel_bindings WHERE business_id = :b AND simulated_only
                          AND channel = 'whatsapp' AND revoked_at IS NULL LIMIT 1"""),
                {"b": business_id},
            )
        ).scalar_one_or_none()
    return ext.lstrip("+") if ext else None


async def owner_says(
    business_id: UUID,
    *,
    text_: str | None = None,
    option_id: str | None = None,
    option_title: str | None = None,
    media: SimMedia | None = None,
) -> SimResult:
    """The simulated owner writes (or taps a button) on WhatsApp — the real ingress, accepted as the owner only
    because the event is signed and flagged simulated."""
    wa_id = await sim_owner_wa_id(business_id)
    if wa_id is None:
        raise LookupError("this business has no simulated owner binding")
    pnid = await whatsapp_number(business_id)
    mid = f"wamid.SIMOWN{new_id().hex}"
    message = _message(mid, text_, option_id, option_title, media)
    raw = json.dumps(_payload(pnid, wa_id, "Owner (Lab)", message)).encode()
    return SimResult(await post_raw(raw), mid, wa_id)


async def owner_thread(business_id: UUID, limit: int = 50) -> dict[str, Any]:
    wa_id = await sim_owner_wa_id(business_id)
    return await thread_by_key(business_id, wa_id, limit) if wa_id else {"conversation_id": None, "items": []}


async def thread_by_key(business_id: UUID, thread_key: str, limit: int = 50) -> dict[str, Any]:
    """Like `thread`, for any simulated WhatsApp thread key (customer or owner device)."""
    async with get_db().tenant_session(business_id) as s:
        conv = (
            await s.execute(
                text("""SELECT id FROM conversations WHERE thread_key = :t AND simulated ORDER BY created_at DESC
                        LIMIT 1"""),
                {"t": thread_key},
            )
        ).scalar_one_or_none()
    if conv is None:
        return {"conversation_id": None, "items": []}
    return await _items(business_id, conv, limit)


def payload_text(payload: dict[str, Any]) -> str:
    """What a person reads in a rendered WhatsApp payload (text, caption or interactive body)."""
    kind = payload.get("type")
    if kind == "text":
        return str((payload.get("text") or {}).get("body", ""))
    if kind == "interactive":
        return str(((payload.get("interactive") or {}).get("body") or {}).get("text", ""))
    if kind in ("image", "document"):
        return str((payload.get(kind) or {}).get("caption", ""))
    return ""


def payload_options(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(id, title) of the buttons or list rows a person can tap."""
    action = (payload.get("interactive") or {}).get("action") or {}
    if "buttons" in action:
        return [(b["reply"]["id"], b["reply"]["title"]) for b in action["buttons"]]
    return [(r["id"], r["title"]) for sec in action.get("sections", []) for r in sec.get("rows", [])]
