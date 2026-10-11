"""Public interface of the `gateway` module. Other modules import only from here (POC_01 §4).

Channel adapters, ingress verification, normalization, identity and roles, conversations/messages, rendering,
outbound, channel policy, link codes. Spec: POC_03.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    ConversationSenderPort,
    InboundProcessorPort,
    MediaUrlPort,
    OutboundSenderPort,
)
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.ports import Registry
from reflexo.common.xray import emit
from reflexo.gateway import identity
from reflexo.gateway.adapters.base import ChannelAdapter
from reflexo.gateway.adapters.meta_messaging import InstagramAdapter, MessengerAdapter
from reflexo.gateway.adapters.meta_whatsapp import WhatsAppAdapter
from reflexo.gateway.adapters.simulator import SimulatorAdapter
from reflexo.gateway.adapters.telegram import TelegramAdapter
from reflexo.gateway.connections import ConnectionInfo, credentials, find_connection, get_connection
from reflexo.gateway.connections import clear_cache as clear_connection_cache
from reflexo.gateway.inbound import GatewayInbound
from reflexo.gateway.media import MediaSigner, file_kind, media_path, register_jobs, store_media
from reflexo.gateway.outbound import EnqueueResult, Outbound
from reflexo.gateway.views import (
    ConversationView,
    CustomerView,
    MessageView,
    add_customer_facts,
    capability_summary,
    conversation_view,
    mark_needs_attention,
    messages_by_ids,
    owner_timeline,
    recent_messages,
    set_customer_language,
)
from reflexo.pipeline.api import store_inbox_event

__all__ = [
    "ConnectionInfo",
    "ConversationView",
    "CustomerView",
    "EnqueueResult",
    "IngestResult",
    "MessageView",
    "add_customer_facts",
    "capability_summary",
    "conversation_view",
    "find_connection",
    "get_connection",
    "ingest_webhook",
    "issue_link_code",
    "mark_needs_attention",
    "media_path",
    "media_signer",
    "messages_by_ids",
    "outbound",
    "recent_messages",
    "register",
    "resolve_customer",
    "set_customer_language",
    "store_media",
]

_adapters: dict[str, ChannelAdapter] = {}
_outbound: Outbound | None = None
_signer = MediaSigner()


def adapters() -> dict[str, ChannelAdapter]:
    if not _adapters:
        client = httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        _adapters.update(
            {
                "meta_whatsapp": WhatsAppAdapter(client),
                "meta_pages": MessengerAdapter(client),
                "meta_instagram": InstagramAdapter(client),
                "telegram": TelegramAdapter(client),
                "simulator": SimulatorAdapter(),
            }
        )
    return _adapters


def register(registry: Registry) -> None:
    global _outbound
    _outbound = Outbound(registry, adapters())
    inbound = GatewayInbound(registry, adapters(), _outbound)
    registry.provide(InboundProcessorPort, inbound)  # type: ignore[type-abstract]
    registry.provide(OutboundSenderPort, _outbound)  # type: ignore[type-abstract]
    registry.provide(MediaUrlPort, _signer)  # type: ignore[type-abstract]
    registry.provide(ConversationSenderPort, ConversationSender())  # type: ignore[type-abstract]
    register_jobs(registry, adapters())


def outbound() -> Outbound:
    """Send API for other modules (agent, automations, owner): `await outbound().enqueue(session, ...)`."""
    if _outbound is None:
        raise RuntimeError("gateway not registered (bootstrap() not called)")
    return _outbound


def media_signer() -> MediaSigner:
    return _signer


# ---------------------------------------------------------------- webhook ingress (POC_03 §2)


@dataclass(frozen=True)
class IngestResult:
    status_code: int
    body: str = ""


async def ingest_webhook(
    provider: str, headers: Mapping[str, str], raw_body: bytes, path_params: Mapping[str, str]
) -> IngestResult:
    """verify → dedupe key → route key → insert inbox (dedupe) → 200. Never 2xx for an unstored event."""
    if len(raw_body) > get_settings().max_webhook_bytes:
        return IngestResult(413, "payload too large")
    adapter = adapters().get(provider)
    if adapter is None:
        return IngestResult(404, "unknown provider")
    lowered = {k.lower(): v for k, v in headers.items()}
    if not adapter.verify(lowered, raw_body):
        await emit("ingress", PipelineEventStatus.ERROR, provider=provider, reason="signature")
        return IngestResult(401, "invalid signature")
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        return IngestResult(400, "invalid json")
    route_key = adapter.route_key(payload, path_params)
    simulated = lowered.get("x-reflexo-simulated") == "1"
    kept_headers = {
        k: v
        for k, v in lowered.items()
        if k
        in (
            "content-type",
            "x-hub-signature-256",
            "x-telegram-bot-api-secret-token",
            "x-reflexo-signature",
            "x-reflexo-simulated",
        )
    }
    # scoped by route key: Telegram update_ids (and simulator event ids) are only unique per bot/connection
    dedupe_key = f"{route_key or '-'}:{adapter.dedupe_key(raw_body, payload)}"
    result = await store_inbox_event(
        provider,
        dedupe_key,
        route_key=route_key,
        headers=kept_headers,
        payload=payload,
        simulated=simulated,
    )
    await emit(
        "inbox",
        PipelineEventStatus.OK if result.inserted else PipelineEventStatus.SKIP,
        provider=provider,
        inbox_id=str(result.inbox_id) if result.inbox_id else None,
        simulated=simulated,
    )
    return IngestResult(200, "{}" if provider == "telegram" else "OK")


def verify_meta_challenge(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    expected = get_settings().meta_verify_token.get_secret_value()
    if mode == "subscribe" and expected and token == expected and challenge:
        return challenge
    return None


async def issue_link_code(
    s: AsyncSession, business_id: UUID, customer_id: UUID, source_channel: str
) -> tuple[str, str]:
    return await identity.issue_link_code(s, business_id, customer_id, source_channel)


async def resolve_customer(
    s: AsyncSession, business_id: UUID, channel: str, external_id: str, display_name: str | None = None
) -> tuple[UUID, bool]:
    """Identity lookup/creation for internal callers (voice caller = same WhatsApp user, POC_03 §5.1)."""
    return await identity.resolve_customer(s, business_id, channel, external_id, display_name, None)


# ---------------------------------------------------------------- calls (voice-bridge, POC_03 §5.1)


async def resolve_caller(
    s: AsyncSession, conn: ConnectionInfo, wa_id: str, display_name: str | None = None
) -> identity.ResolvedSender:
    """A WhatsApp caller is the same person as the WhatsApp chat sender: same customer, same chat conversation
    (the call is linked to it), and owner bindings apply exactly as for messages."""
    from datetime import UTC, datetime

    from reflexo.common.enums import ChannelProvider, ChannelType
    from reflexo.gateway.adapters.meta_common import wa_id_to_e164
    from reflexo.gateway.normalized import NormalizedEvent

    ev = NormalizedEvent(
        kind="call",
        provider=ChannelProvider(conn.provider),
        channel=ChannelType(conn.channel),
        route_key=conn.external_account_id,
        occurred_at=datetime.now(UTC),
        thread_key=wa_id,
        sender_external_id=wa_id_to_e164(wa_id),
        sender_display_name=display_name,
    )
    return await identity.resolve(s, conn, ev, simulated=False)


__all__ += ["ResolvedSender", "credentials", "resolve_caller"]
ResolvedSender = identity.ResolvedSender


# ---------------------------------------------------------------- sending outside a turn (ConversationSenderPort)


class ConversationSender:
    async def send_blocks(
        self,
        business_id: UUID,
        conversation_id: UUID,
        blocks: list[dict[str, object]],
        *,
        origin: str,
        idempotency_base: str,
    ) -> UUID | None:
        from reflexo.common.db import get_db

        async with get_db().tenant_session(business_id) as s:
            result = await outbound().enqueue(
                s,
                business_id=business_id,
                conversation_id=conversation_id,
                blocks=blocks,
                origin=origin,
                idempotency_base=idempotency_base,
            )
        return result.message_id


async def member_conversation(s: AsyncSession, conn: ConnectionInfo, thread_key: str, member_id: UUID) -> UUID:
    """The owner/team member's conversation on a connection (created on first use, e.g. for notifications)."""
    conv_id, _, _ = await identity._conversation(s, conn, thread_key, "owner", None, member_id, False)
    return conv_id


__all__ += ["ConversationSender", "member_conversation"]


async def set_customer_mode(business_id: UUID, mode: str) -> int:
    """`/pause` (off) and `/resume` (tiered): the AI's mode on every customer-facing connection of the business."""
    async with get_db().tenant_session(business_id) as s:
        n = (
            await s.execute(
                text("""UPDATE channel_connections SET customer_mode = CAST(:m AS customer_mode)
                        WHERE 'customer' = ANY(roles) RETURNING id"""),
                {"m": mode},
            )
        ).all()
    clear_connection_cache()
    return len(n)


__all__ += ["set_customer_mode"]

__all__ += ["owner_timeline"]


async def set_media_description(business_id: UUID, media_object_id: UUID, description: str) -> None:
    """Filled by the media.describe job; shown wherever the attachment appears ([image: …] in prompts)."""
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("UPDATE media_objects SET description = :d WHERE id = :id"),
            {"d": description[:1000], "id": media_object_id},
        )


__all__ += ["file_kind", "set_media_description"]
