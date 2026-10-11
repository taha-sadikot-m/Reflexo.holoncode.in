"""WhatsApp Cloud API adapter (harvested H2, H4, H8 from the old Meta MCP; rebuilt stateless and typed).

Inbound: `whatsapp_business_account` webhooks — `messages` (messages + statuses) and `calls` (call signals).
Outbound: text, image, document, interactive button (≤3) / list (≤10 rows), template.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from reflexo.common.config import get_settings
from reflexo.common.contracts import SendResult
from reflexo.common.enums import ChannelProvider, ChannelType
from reflexo.gateway.adapters.base import (
    Credentials,
    IRCards,
    IRChoices,
    IRFile,
    IRImage,
    IRLink,
    IRPart,
    IRText,
    RenderContext,
)
from reflexo.gateway.adapters.meta_common import post_json, verify_signature, wa_id_to_e164
from reflexo.gateway.manifests import manifest_for
from reflexo.gateway.normalized import (
    CallSignal,
    DeliveryStatus,
    GeoPoint,
    InboundMedia,
    InteractiveReply,
    NormalizedEvent,
)

_MEDIA_TYPES = ("image", "document", "audio", "video", "sticker")


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class WhatsAppAdapter:
    provider = ChannelProvider.META_WHATSAPP
    manifest = manifest_for("meta_whatsapp")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    # ---------------------------------------------------------------- ingress
    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        secret = get_settings().meta_app_secret.get_secret_value()
        return verify_signature(raw_body, headers.get("x-hub-signature-256"), secret)

    def dedupe_key(self, raw_body: bytes, payload: dict[str, Any]) -> str:
        return hashlib.sha256(raw_body).hexdigest()

    def route_key(self, payload: dict[str, Any], path_params: Mapping[str, str]) -> str | None:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                pnid = (change.get("value") or {}).get("metadata", {}).get("phone_number_id")
                if pnid:
                    return str(pnid)
        return None

    def parse(self, payload: dict[str, Any], route_key: str) -> list[NormalizedEvent]:
        if payload.get("object") != "whatsapp_business_account":
            return []
        events: list[NormalizedEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                pnid = str((value.get("metadata") or {}).get("phone_number_id") or route_key)
                field = change.get("field")
                if field == "messages":
                    events += self._parse_messages(value, pnid)
                elif field == "calls":
                    events += self._parse_calls(value, pnid)
        return events

    def _parse_messages(self, value: dict[str, Any], pnid: str) -> list[NormalizedEvent]:
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
        out: list[NormalizedEvent] = []
        for msg in value.get("messages", []):
            wa_id = str(msg.get("from", ""))
            mtype = msg.get("type")
            base: dict[str, Any] = {
                "provider": self.provider,
                "channel": ChannelType.WHATSAPP,
                "route_key": pnid,
                "occurred_at": _ts(msg.get("timestamp")),
                "thread_key": wa_id,
                "external_message_id": msg.get("id"),
                "sender_external_id": wa_id_to_e164(wa_id),
                "sender_display_name": names.get(wa_id),
                "raw": msg,
            }
            if mtype == "text":
                out.append(NormalizedEvent(kind="message", text=(msg.get("text") or {}).get("body"), **base))
            elif mtype in _MEDIA_TYPES:
                m = msg.get(mtype) or {}
                media = InboundMedia(
                    provider_media_id=str(m.get("id")),
                    kind=mtype,
                    mime=m.get("mime_type"),
                    filename=m.get("filename"),
                    caption=m.get("caption"),
                )
                out.append(NormalizedEvent(kind="message", text=m.get("caption"), media=[media], **base))
            elif mtype == "location":
                loc = msg.get("location") or {}
                point = GeoPoint(
                    latitude=float(loc.get("latitude", 0)),
                    longitude=float(loc.get("longitude", 0)),
                    name=loc.get("name"),
                    address=loc.get("address"),
                )
                out.append(
                    NormalizedEvent(kind="message", location=point, text=loc.get("address") or loc.get("name"), **base)
                )
            elif mtype == "interactive":
                inter = msg.get("interactive") or {}
                if inter.get("type") == "call_permission_reply":
                    # WhatsApp grants call-back permission automatically after a customer calls us; it arrives as
                    # an "interactive" message but it is a call-domain event, never something to reply to.
                    perm = inter.get("call_permission_reply") or {}
                    call_id = str((msg.get("context") or {}).get("id") or msg.get("id") or "")
                    out.append(
                        NormalizedEvent(
                            kind="call",
                            provider=self.provider,
                            channel=ChannelType.VOICE,
                            route_key=pnid,
                            occurred_at=_ts(msg.get("timestamp")),
                            thread_key=wa_id,
                            sender_external_id=wa_id_to_e164(wa_id),
                            call=CallSignal(call_id=call_id, event="permission_reply", caller_wa_id=wa_id, raw=perm),
                            raw=msg,
                        )
                    )
                    continue
                reply = inter.get("button_reply") or inter.get("list_reply") or {}
                if not reply.get("id"):
                    continue  # other interactive types (flows, unknown future types) carry no option to act on
                out.append(
                    NormalizedEvent(
                        kind="interactive_reply",
                        text=reply.get("title"),
                        interactive=InteractiveReply(option_id=str(reply.get("id", "")), title=reply.get("title")),
                        **base,
                    )
                )
            elif mtype == "button":  # template quick-reply button
                btn = msg.get("button") or {}
                out.append(
                    NormalizedEvent(
                        kind="interactive_reply",
                        text=btn.get("text"),
                        interactive=InteractiveReply(option_id=str(btn.get("payload") or ""), title=btn.get("text")),
                        **base,
                    )
                )
            elif mtype == "contacts":
                contacts = msg.get("contacts") or []
                summary = ", ".join(
                    f"{(c.get('name') or {}).get('formatted_name', '')} {(c.get('phones') or [{}])[0].get('phone', '')}"
                    for c in contacts
                )
                out.append(NormalizedEvent(kind="message", text=f"[shared contact] {summary}".strip(), **base))
            # reactions, ephemeral and unsupported types are ignored deliberately
        for st in value.get("statuses", []):
            status = st.get("status")
            if status not in ("sent", "delivered", "read", "failed"):
                continue
            errors = st.get("errors") or [{}]
            out.append(
                NormalizedEvent(
                    kind="status",
                    provider=self.provider,
                    channel=ChannelType.WHATSAPP,
                    route_key=pnid,
                    occurred_at=_ts(st.get("timestamp")),
                    thread_key=str(st.get("recipient_id", "")),
                    status=DeliveryStatus(
                        provider_message_id=str(st.get("id")),
                        status=status,
                        error_code=str(errors[0].get("code")) if status == "failed" else None,
                        error_message=errors[0].get("title") if status == "failed" else None,
                    ),
                    raw=st,
                )
            )
        return out

    def _parse_calls(self, value: dict[str, Any], pnid: str) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        for call in value.get("calls", []):
            session = call.get("session") or {}
            caller = str(call.get("from", ""))
            out.append(
                NormalizedEvent(
                    kind="call",
                    provider=self.provider,
                    channel=ChannelType.VOICE,
                    route_key=pnid,
                    occurred_at=_ts(call.get("timestamp")),
                    thread_key=caller,
                    sender_external_id=wa_id_to_e164(caller) if caller else None,
                    call=CallSignal(
                        call_id=str(call.get("id")),
                        event=str(call.get("event", "")),
                        sdp=session.get("sdp"),
                        sdp_type=session.get("sdp_type"),
                        caller_wa_id=caller or None,
                        raw=call,
                    ),
                    raw=call,
                )
            )
        return out

    # ---------------------------------------------------------------- egress
    def to_payloads(self, parts: list[IRPart], ctx: RenderContext) -> list[dict[str, Any]]:
        if ctx.template is not None:
            return [self._template(ctx)]
        payloads: list[dict[str, Any]] = []
        for part in parts:
            body = self._part(part)
            if body is None:
                continue
            payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": ctx.recipient, **body}
            if ctx.reply_to_external_id and not payloads:
                payload["context"] = {"message_id": ctx.reply_to_external_id}
            payloads.append(payload)
        return payloads

    def _part(self, part: IRPart) -> dict[str, Any] | None:
        m = self.manifest
        if isinstance(part, IRText):
            return {"type": "text", "text": {"body": part.text[: m.max_text], "preview_url": True}}
        if isinstance(part, IRImage):
            image: dict[str, Any] = {"link": part.url}
            if part.caption:
                image["caption"] = part.caption[: m.caption_max]
            return {"type": "image", "image": image}
        if isinstance(part, IRFile):
            doc: dict[str, Any] = {"link": part.url, "filename": part.filename}
            if part.caption:
                doc["caption"] = part.caption[: m.caption_max]
            return {"type": "document", "document": doc}
        if isinstance(part, IRChoices) and part.style in ("buttons", "quick_replies") and len(part.options) <= 3:
            return {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": part.text[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": o.id[:256], "title": o.title[:20]}} for o in part.options
                        ]
                    },
                },
            }
        if isinstance(part, IRChoices):
            rows = [{"id": o.id[:200], "title": o.title[:24]} for o in part.options[: m.list_max_rows]]
            return {
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": part.text[:1024]},
                    "action": {
                        "button": part.button_label[:20] or "Options",
                        "sections": [{"title": "Options", "rows": rows}],
                    },
                },
            }
        if isinstance(part, IRLink):
            return {"type": "text", "text": {"body": f"{part.text}\n{part.url}"[: m.max_text], "preview_url": True}}
        if isinstance(part, IRCards):  # not supported natively; renderer degrades carousels before this point
            lines = [f"• {v.name}" for v in part.items]
            return {"type": "text", "text": {"body": "\n".join(lines)}}
        return None

    def _template(self, ctx: RenderContext) -> dict[str, Any]:
        tpl = ctx.template or {}
        params = [{"type": "text", "text": str(v)[:1024]} for v in tpl.get("vars", [])]
        return {
            "messaging_product": "whatsapp",
            "to": ctx.recipient,
            "type": "template",
            "template": {
                "name": tpl["name"],
                "language": {"code": tpl.get("language", "en")},
                "components": [{"type": "body", "parameters": params}] if params else [],
            },
        }

    def _url(self, path: str) -> str:
        s = get_settings()
        return f"{s.meta_graph_base}/{s.meta_graph_version}/{path}"

    async def send(self, payload: dict[str, Any], creds: Credentials) -> SendResult:
        return await post_json(
            self._client,
            self._url(f"{creds.account_id}/messages"),
            payload,
            token=creds.token,
            id_path=("messages", 0, "id"),
        )

    async def mark_read_and_typing(self, creds: Credentials, recipient: str, external_message_id: str | None) -> None:
        if not external_message_id:
            return
        body = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": external_message_id,
            "typing_indicator": {"type": "text"},
        }  # (verify) typing indicator shape in current Cloud API
        await post_json(
            self._client, self._url(f"{creds.account_id}/messages"), body, token=creds.token, id_path=("success",)
        )

    async def download_media(self, creds: Credentials, media: InboundMedia) -> tuple[bytes, str]:
        headers = {"Authorization": f"Bearer {creds.token}"}
        meta = await self._client.get(self._url(media.provider_media_id), headers=headers, timeout=10.0)
        meta.raise_for_status()
        info = meta.json()
        data = await self._client.get(info["url"], headers=headers, timeout=30.0)
        data.raise_for_status()
        return data.content, str(info.get("mime_type") or media.mime or "application/octet-stream")
