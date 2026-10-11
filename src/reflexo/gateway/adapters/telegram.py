"""Telegram Bot API adapter (POC_03 §10.3). Owner and customers can use the same bot; the role comes from bindings.

Outbound uses HTML parse mode with escaping (Markdown breaks on arbitrary user/product text). `*bold*` written by
the renderer (WhatsApp style) is converted to `<b>`. Inline keyboard callbacks carry `o:<option_id>` (≤ 64 bytes).
A retry after an unknown outcome can duplicate a message on Telegram, so it is disabled for this provider.
"""

from __future__ import annotations

import hmac
import html
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from reflexo.common.config import get_settings
from reflexo.common.contracts import SendResult, SendStatus
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
from reflexo.gateway.manifests import manifest_for
from reflexo.gateway.normalized import GeoPoint, InboundMedia, InteractiveReply, NormalizedEvent

TIMEOUT = httpx.Timeout(10.0, connect=3.0)
CALLBACK_PREFIX = "o:"
_BOLD = re.compile(r"\*([^*\n]+)\*")


def to_html(text: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))


def _callback(option_id: str) -> str:
    data = CALLBACK_PREFIX + option_id
    return data.encode("utf-8")[:64].decode("utf-8", "ignore")


class TelegramAdapter:
    provider = ChannelProvider.TELEGRAM
    manifest = manifest_for("telegram")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    # ---------------------------------------------------------------- ingress
    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        secret = get_settings().telegram_webhook_secret.get_secret_value()
        given = headers.get("x-telegram-bot-api-secret-token") or ""
        return bool(secret) and hmac.compare_digest(given, secret)

    def dedupe_key(self, raw_body: bytes, payload: dict[str, Any]) -> str:
        return str(payload.get("update_id"))

    def route_key(self, payload: dict[str, Any], path_params: Mapping[str, str]) -> str | None:
        return path_params.get("connection_id")

    def parse(self, payload: dict[str, Any], route_key: str) -> list[NormalizedEvent]:
        if "callback_query" in payload:
            cq = payload["callback_query"]
            data = str(cq.get("data") or "")
            chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            user = cq.get("from") or {}
            option = data[len(CALLBACK_PREFIX) :] if data.startswith(CALLBACK_PREFIX) else data
            return [
                NormalizedEvent(
                    kind="interactive_reply",
                    provider=self.provider,
                    channel=ChannelType.TELEGRAM,
                    route_key=route_key,
                    occurred_at=datetime.now(UTC),
                    thread_key=chat_id,
                    external_message_id=f"cb:{cq.get('id')}",
                    sender_external_id=str(user.get("id", "")),
                    sender_display_name=user.get("first_name"),
                    interactive=InteractiveReply(option_id=option),
                    raw={"callback_query_id": cq.get("id"), **payload},
                )
            ]
        msg = payload.get("message") or payload.get("edited_message")
        if not msg:
            return []
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        user = msg.get("from") or {}
        text = msg.get("text") or msg.get("caption")
        media: list[InboundMedia] = []
        if msg.get("photo"):
            biggest = msg["photo"][-1]
            media.append(
                InboundMedia(
                    provider_media_id=biggest["file_id"], kind="image", mime="image/jpeg", caption=msg.get("caption")
                )
            )
        for key, kind in (("document", "document"), ("voice", "audio"), ("audio", "audio"), ("video", "video")):
            if msg.get(key):
                item = msg[key]
                media.append(
                    InboundMedia(
                        provider_media_id=item["file_id"],
                        kind=kind,
                        mime=item.get("mime_type"),
                        filename=item.get("file_name"),
                    )
                )
        location = None
        if msg.get("location"):
            loc = msg["location"]
            location = GeoPoint(latitude=loc["latitude"], longitude=loc["longitude"])
        referral = None
        if text and text.startswith("/start "):
            referral = text.split(" ", 1)[1].strip() or None
        if msg.get("contact"):
            c = msg["contact"]
            text = f"[shared contact] {c.get('first_name', '')} {c.get('phone_number', '')}".strip()
        return [
            NormalizedEvent(
                kind="message",
                provider=self.provider,
                channel=ChannelType.TELEGRAM,
                route_key=route_key,
                occurred_at=datetime.fromtimestamp(int(msg.get("date", 0)), tz=UTC)
                if msg.get("date")
                else datetime.now(UTC),
                thread_key=chat_id,
                external_message_id=str(msg.get("message_id")),
                sender_external_id=str(user.get("id", "")),
                sender_display_name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None,
                text=text,
                media=media,
                location=location,
                referral_code=referral,
                reply_to_external_id=str(msg["reply_to_message"]["message_id"])
                if (msg.get("reply_to_message") or {}).get("message_id")
                else None,
                raw=msg,
            )
        ]

    # ---------------------------------------------------------------- egress
    def to_payloads(self, parts: list[IRPart], ctx: RenderContext) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for part in parts:
            p = self._part(part, ctx.recipient)
            if p:
                out.append(p)
        return out

    def _part(self, part: IRPart, chat_id: str) -> dict[str, Any] | None:
        if isinstance(part, IRText):
            return {"method": "sendMessage", "chat_id": chat_id, "text": to_html(part.text), "parse_mode": "HTML"}
        if isinstance(part, IRImage):
            body: dict[str, Any] = {"method": "sendPhoto", "chat_id": chat_id, "photo": part.url}
            if part.caption:
                body |= {"caption": to_html(part.caption)[:1024], "parse_mode": "HTML"}
            return body
        if isinstance(part, IRFile):
            return {
                "method": "sendDocument",
                "chat_id": chat_id,
                "document": part.url,
                **({"caption": to_html(part.caption)[:1024], "parse_mode": "HTML"} if part.caption else {}),
            }
        if isinstance(part, IRChoices):
            keyboard = [[{"text": o.title[:64], "callback_data": _callback(o.id)}] for o in part.options]
            return {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": to_html(part.text),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": keyboard},
            }
        if isinstance(part, IRLink):
            return {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": to_html(part.text),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[{"text": part.label[:64], "url": part.url}]]},
            }
        if isinstance(part, IRCards):
            lines = [f"• <b>{html.escape(v.name)}</b>" for v in part.items]
            return {"method": "sendMessage", "chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"}
        return None

    def _api(self, token: str, method: str) -> str:
        return f"{get_settings().telegram_api_base}/bot{token}/{method}"

    async def send(self, payload: dict[str, Any], creds: Credentials) -> SendResult:
        body = dict(payload)
        method = body.pop("method")
        try:
            resp = await self._client.post(self._api(creds.token, method), json=body, timeout=TIMEOUT)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            return SendResult(SendStatus.RETRYABLE, error_code="PROVIDER_UNAVAILABLE", error_message=type(exc).__name__)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            return SendResult(
                SendStatus.UNKNOWN,
                error_code="TIMEOUT",
                error_message=type(exc).__name__,
                retry_after_unknown_allowed=False,
            )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code < 300 and data.get("ok"):
            return SendResult(SendStatus.SENT, provider_message_id=str((data.get("result") or {}).get("message_id")))
        desc = str(data.get("description") or resp.text[:300])
        if resp.status_code == 429:
            retry = float((data.get("parameters") or {}).get("retry_after", 5))
            return SendResult(SendStatus.RETRYABLE, error_code="RATE_LIMITED", error_message=desc, retry_after_s=retry)
        if resp.status_code >= 500:
            return SendResult(SendStatus.RETRYABLE, error_code="PROVIDER_UNAVAILABLE", error_message=desc)
        if resp.status_code == 401:
            return SendResult(SendStatus.PERMANENT, error_code="CREDENTIAL_INVALID", error_message=desc)
        if resp.status_code == 403:
            return SendResult(SendStatus.PERMANENT, error_code="RECIPIENT_UNREACHABLE", error_message=desc)
        return SendResult(SendStatus.PERMANENT, error_code="PROVIDER_ERROR", error_message=desc)

    async def mark_read_and_typing(self, creds: Credentials, recipient: str, external_message_id: str | None) -> None:
        await self._client.post(
            self._api(creds.token, "sendChatAction"), json={"chat_id": recipient, "action": "typing"}, timeout=TIMEOUT
        )

    async def acknowledge_interaction(self, creds: Credentials, callback_query_id: str) -> None:
        await self._client.post(
            self._api(creds.token, "answerCallbackQuery"),
            json={"callback_query_id": callback_query_id},
            timeout=TIMEOUT,
        )

    async def download_media(self, creds: Credentials, media: InboundMedia) -> tuple[bytes, str]:
        meta = await self._client.post(
            self._api(creds.token, "getFile"), json={"file_id": media.provider_media_id}, timeout=TIMEOUT
        )
        meta.raise_for_status()
        path = meta.json()["result"]["file_path"]
        data = await self._client.get(f"{get_settings().telegram_api_base}/file/bot{creds.token}/{path}", timeout=30.0)
        data.raise_for_status()
        return data.content, media.mime or data.headers.get("content-type", "application/octet-stream")
