"""Simulator adapter and transport (POC_03 §10.6–10.7).

* As a channel (Kurti Kraft, Lab-only customers): signed JSON envelopes in, full-fidelity parts out.
* As a transport: for conversations created from signed *simulated* events on real providers (fake wa_id on
  Meera's WhatsApp), the real adapter renders the provider payload and this transport "delivers" it to the
  Lab device stream instead of calling Meta/Telegram. Everything before the final HTTP call is the real path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from reflexo.common.config import get_settings
from reflexo.common.contracts import SendResult, SendStatus
from reflexo.common.enums import ChannelProvider, ChannelType, PipelineEventStatus
from reflexo.common.ids import new_id
from reflexo.common.xray import emit
from reflexo.gateway.adapters.base import Credentials, IRPart, RenderContext
from reflexo.gateway.manifests import manifest_for
from reflexo.gateway.normalized import InboundMedia, InteractiveReply, NormalizedEvent


def sign(raw_body: bytes) -> str:
    secret = get_settings().simulator_secret.get_secret_value()
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def _jsonable(part: Any) -> Any:
    if is_dataclass(part) and not isinstance(part, type):
        data = asdict(part)
        data["part"] = type(part).__name__
        return json.loads(json.dumps(data, default=str))
    return part


class SimulatorAdapter:
    provider = ChannelProvider.SIMULATOR
    manifest = manifest_for("simulator")

    # ---------------------------------------------------------------- ingress
    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        given = headers.get("x-reflexo-signature") or ""
        if not get_settings().simulator_secret.get_secret_value():
            return False  # fail closed: an empty key would make every signature forgeable
        return bool(given) and hmac.compare_digest(given, sign(raw_body))

    def dedupe_key(self, raw_body: bytes, payload: dict[str, Any]) -> str:
        return str(payload.get("event_id") or hashlib.sha256(raw_body).hexdigest())

    def route_key(self, payload: dict[str, Any], path_params: Mapping[str, str]) -> str | None:
        return path_params.get("connection_id")

    def parse(self, payload: dict[str, Any], route_key: str) -> list[NormalizedEvent]:
        sender = payload.get("sender") or {}
        media = [
            InboundMedia(
                provider_media_id=m.get("url", ""),
                kind=m.get("kind", "image"),
                mime=m.get("mime"),
                url=m.get("url"),
                filename=m.get("filename"),
            )
            for m in payload.get("media", [])
        ]
        interactive = payload.get("interactive")
        return [
            NormalizedEvent(
                kind="interactive_reply" if interactive else "message",
                provider=self.provider,
                channel=ChannelType.WEB_WIDGET,
                route_key=route_key,
                occurred_at=datetime.now(UTC),
                thread_key=str(sender.get("id")),
                external_message_id=str(payload.get("event_id") or new_id()),
                sender_external_id=str(sender.get("id")),
                sender_display_name=sender.get("name"),
                text=payload.get("text"),
                media=media,
                interactive=InteractiveReply(**interactive) if interactive else None,
                raw=payload,
            )
        ]

    # ---------------------------------------------------------------- egress
    def to_payloads(self, parts: list[IRPart], ctx: RenderContext) -> list[dict[str, Any]]:
        return [{"to": ctx.recipient, "part": _jsonable(p)} for p in parts]

    async def send(self, payload: dict[str, Any], creds: Credentials) -> SendResult:
        return await deliver_to_lab(payload, business_id=None, conversation_id=None)

    async def mark_read_and_typing(self, creds: Credentials, recipient: str, external_message_id: str | None) -> None:
        return None

    async def download_media(self, creds: Credentials, media: InboundMedia) -> tuple[bytes, str]:
        raise LookupError("simulator media is referenced by URL and fetched by the media job")


async def deliver_to_lab(
    payload: dict[str, Any], *, business_id: UUID | None, conversation_id: UUID | None, provider: str = "simulator"
) -> SendResult:
    """Simulator transport: publish the rendered provider payload to the Lab device stream."""
    message_id = f"sim-{new_id()}"
    await emit(
        "sim_message",
        PipelineEventStatus.OK,
        business_id=business_id,
        conversation_id=conversation_id,
        provider=provider,
        provider_message_id=message_id,
        payload=payload,
    )
    return SendResult(SendStatus.SENT, provider_message_id=message_id)
