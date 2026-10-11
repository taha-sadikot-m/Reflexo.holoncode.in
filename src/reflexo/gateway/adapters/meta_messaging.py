"""Messenger and Instagram adapters (harvested H3, H4). Both use the Messenger Platform `messaging[]` shape for
DMs; comments arrive as `changes` (`feed` for Pages, `comments` for Instagram)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

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
    Manifest,
    RenderContext,
)
from reflexo.gateway.adapters.meta_common import post_json, verify_signature
from reflexo.gateway.manifests import manifest_for
from reflexo.gateway.normalized import CommentInfo, InboundMedia, InteractiveReply, NormalizedEvent

_ATTACHMENT_KINDS: dict[str, Literal["image", "video", "audio", "document"]] = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "file": "document",
}


def _ms(value: Any) -> datetime:
    try:
        v = int(value)
        return datetime.fromtimestamp(v / 1000 if v > 10_000_000_000 else v, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class _MessagingAdapter:
    provider: ChannelProvider
    manifest: Manifest
    dm_channel: ChannelType
    comment_channel: ChannelType
    object_name: str
    comment_fields: frozenset[str]

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    # ---------------------------------------------------------------- ingress
    def _secret(self) -> str:
        return get_settings().meta_app_secret.get_secret_value()

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return verify_signature(raw_body, headers.get("x-hub-signature-256"), self._secret())

    def dedupe_key(self, raw_body: bytes, payload: dict[str, Any]) -> str:
        return hashlib.sha256(raw_body).hexdigest()

    def route_key(self, payload: dict[str, Any], path_params: Mapping[str, str]) -> str | None:
        entries = payload.get("entry") or []
        return str(entries[0].get("id")) if entries and entries[0].get("id") else None

    def parse(self, payload: dict[str, Any], route_key: str) -> list[NormalizedEvent]:
        if payload.get("object") != self.object_name:
            return []
        events: list[NormalizedEvent] = []
        for entry in payload.get("entry", []):
            account_id = str(entry.get("id") or route_key)
            for item in entry.get("messaging", []):
                ev = self._parse_messaging(item, account_id)
                if ev is not None:
                    events.append(ev)
            for change in entry.get("changes", []):
                field, value = change.get("field"), change.get("value") or {}
                if field == "messages" and value:  # Instagram Login style: one messaging item per change
                    ev = self._parse_messaging(value, account_id)
                    if ev is not None:
                        events.append(ev)
                elif field in self.comment_fields:
                    ev = self._parse_comment(value, account_id)
                    if ev is not None:
                        events.append(ev)
        return events

    def _parse_messaging(self, item: dict[str, Any], account_id: str) -> NormalizedEvent | None:
        sender = str((item.get("sender") or {}).get("id", ""))
        recipient = str((item.get("recipient") or {}).get("id", ""))
        base: dict[str, Any] = {
            "provider": self.provider,
            "channel": self.dm_channel,
            "route_key": account_id,
            "occurred_at": _ms(item.get("timestamp")),
            "raw": item,
        }
        if "postback" in item:
            pb = item["postback"] or {}
            return NormalizedEvent(
                kind="interactive_reply",
                thread_key=sender,
                sender_external_id=sender,
                external_message_id=pb.get("mid"),
                text=pb.get("title"),
                interactive=InteractiveReply(option_id=str(pb.get("payload", "")), title=pb.get("title")),
                **base,
            )
        msg = item.get("message")
        if not msg:
            return None  # delivery / read / reaction receipts are not needed for the POC
        if msg.get("is_echo"):
            # Sent by the business: by a human in the inbox (takeover) or by an app (often ourselves).
            return NormalizedEvent(
                kind="echo",
                thread_key=recipient,
                sender_external_id=recipient,
                external_message_id=msg.get("mid"),
                text=msg.get("text"),
                echo_app_id=str(msg["app_id"]) if msg.get("app_id") else None,
                **base,
            )
        media = [
            InboundMedia(
                provider_media_id=str(a.get("payload", {}).get("url") or ""),
                kind=_ATTACHMENT_KINDS.get(a.get("type", ""), "document"),
                url=(a.get("payload") or {}).get("url"),
            )
            for a in msg.get("attachments", [])
            if a.get("type") in _ATTACHMENT_KINDS
        ]
        quick = msg.get("quick_reply") or {}
        if quick.get("payload"):
            return NormalizedEvent(
                kind="interactive_reply",
                thread_key=sender,
                sender_external_id=sender,
                external_message_id=msg.get("mid"),
                text=msg.get("text"),
                interactive=InteractiveReply(option_id=str(quick["payload"]), title=msg.get("text")),
                **base,
            )
        ref = (item.get("referral") or msg.get("referral") or {}).get("ref")
        return NormalizedEvent(
            kind="message",
            thread_key=sender,
            sender_external_id=sender,
            external_message_id=msg.get("mid"),
            text=msg.get("text"),
            media=media,
            referral_code=ref,
            **base,
        )

    def _parse_comment(self, value: dict[str, Any], account_id: str) -> NormalizedEvent | None:
        raise NotImplementedError

    # ---------------------------------------------------------------- egress
    def _recipient(self, ctx: RenderContext) -> dict[str, str]:
        return {"comment_id": ctx.comment_id} if ctx.comment_id else {"id": ctx.recipient}

    def to_payloads(self, parts: list[IRPart], ctx: RenderContext) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for part in parts:
            message = self._message(part)
            if message is not None:
                out.append({"recipient": self._recipient(ctx), "messaging_type": "RESPONSE", "message": message})
        return out

    def _message(self, part: IRPart) -> dict[str, Any] | None:
        m = self.manifest
        if isinstance(part, IRText):
            return {"text": part.text[: m.max_text]}
        if isinstance(part, IRImage):
            return {"attachment": {"type": "image", "payload": {"url": part.url, "is_reusable": True}}}
        if isinstance(part, IRFile):
            return {"attachment": {"type": "file", "payload": {"url": part.url, "is_reusable": True}}}
        if isinstance(part, IRChoices) and part.style == "buttons" and m.max_buttons:
            return {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": part.text[:640],
                        "buttons": [
                            {"type": "postback", "title": o.title[:20], "payload": o.id} for o in part.options[:3]
                        ],
                    },
                }
            }
        if isinstance(part, IRChoices):
            return {
                "text": part.text[: m.max_text],
                "quick_replies": [
                    {"content_type": "text", "title": o.title[:20], "payload": o.id}
                    for o in part.options[: m.quick_replies_max]
                ],
            }
        if isinstance(part, IRLink):
            if m.link_button:
                return {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": part.text[:640],
                            "buttons": [{"type": "web_url", "url": part.url, "title": part.label[:20]}],
                        },
                    }
                }
            return {"text": f"{part.text}\n{part.url}"[: m.max_text]}
        if isinstance(part, IRCards):
            elements = [
                {
                    "title": v.name[:80],
                    "subtitle": "; ".join(f"{line.name}: {line.price_display}" for line in v.lines)[:80] or None,
                    **({"image_url": v.image_url} if v.image_url else {}),
                    "buttons": [
                        {"type": "postback", "title": part.cta_label[:20], "payload": f"order:{v.offering_id}"}
                    ],
                }
                for v in part.items[: m.carousel_max]
            ]
            return {"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": elements}}}
        return None

    def _send_url(self, creds: Credentials) -> str:
        raise NotImplementedError

    async def send(self, payload: dict[str, Any], creds: Credentials) -> SendResult:
        return await post_json(self._client, self._send_url(creds), payload, token=creds.token, id_path=("message_id",))

    async def mark_read_and_typing(self, creds: Credentials, recipient: str, external_message_id: str | None) -> None:
        for action in ("mark_seen", "typing_on"):
            await post_json(
                self._client,
                self._send_url(creds),
                {"recipient": {"id": recipient}, "sender_action": action},
                token=creds.token,
                id_path=("recipient_id",),
            )

    async def download_media(self, creds: Credentials, media: InboundMedia) -> tuple[bytes, str]:
        url = media.url or media.provider_media_id
        resp = await self._client.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", media.mime or "application/octet-stream")


class MessengerAdapter(_MessagingAdapter):
    provider = ChannelProvider.META_PAGES
    manifest = manifest_for("meta_pages")
    dm_channel = ChannelType.FACEBOOK_MESSENGER
    comment_channel = ChannelType.FACEBOOK_COMMENT
    object_name = "page"
    comment_fields = frozenset({"feed"})

    def _parse_comment(self, value: dict[str, Any], account_id: str) -> NormalizedEvent | None:
        if value.get("item") != "comment" or value.get("verb") != "add":
            return None
        author = value.get("from") or {}
        if str(author.get("id")) == account_id or not str(value.get("message") or "").strip():
            return None  # our own comment, or empty
        return NormalizedEvent(
            kind="comment",
            provider=self.provider,
            channel=self.comment_channel,
            route_key=account_id,
            occurred_at=_ms(value.get("created_time")),
            thread_key=str(value.get("comment_id")),
            external_message_id=str(value.get("comment_id")),
            sender_external_id=str(author.get("id")),
            sender_display_name=author.get("name"),
            text=value.get("message"),
            comment=CommentInfo(
                comment_id=str(value.get("comment_id")), post_id=value.get("post_id"), parent_id=value.get("parent_id")
            ),
            raw=value,
        )

    def _send_url(self, creds: Credentials) -> str:
        s = get_settings()
        return f"{s.meta_graph_base}/{s.meta_graph_version}/{creds.account_id}/messages"


class InstagramAdapter(_MessagingAdapter):
    provider = ChannelProvider.META_INSTAGRAM
    manifest = manifest_for("meta_instagram")
    dm_channel = ChannelType.INSTAGRAM_DM
    comment_channel = ChannelType.INSTAGRAM_COMMENT
    object_name = "instagram"
    comment_fields = frozenset({"comments", "feed"})

    def _secret(self) -> str:
        s = get_settings()
        return s.ig_app_secret.get_secret_value() or s.meta_app_secret.get_secret_value()

    def _parse_comment(self, value: dict[str, Any], account_id: str) -> NormalizedEvent | None:
        nested = value.get("value")
        data: dict[str, Any] = nested if isinstance(nested, dict) and "from" not in value else value
        author = data.get("from") or {}
        if str(author.get("id")) == account_id or not str(data.get("text") or "").strip():
            return None
        media = data.get("media") or {}
        comment_id = str(data.get("id"))
        return NormalizedEvent(
            kind="comment",
            provider=self.provider,
            channel=self.comment_channel,
            route_key=account_id,
            occurred_at=_ms(data.get("timestamp") or data.get("created_time")),
            thread_key=comment_id,
            external_message_id=comment_id,
            sender_external_id=str(author.get("id")),
            sender_display_name=author.get("username"),
            text=data.get("text"),
            comment=CommentInfo(
                comment_id=comment_id, post_id=media.get("id") or data.get("media_id"), parent_id=data.get("parent_id")
            ),
            raw=data,
        )

    def _send_url(self, creds: Credentials) -> str:
        s = get_settings()  # Instagram API with Instagram Login (verify host for Facebook-Login apps)
        return f"{s.instagram_graph_base}/{s.meta_graph_version}/{creds.account_id}/messages"
