"""Minimal row factories for integration tests (service role, explicit ids)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id


@dataclass
class Tenant:
    business_id: UUID
    connection_id: UUID
    customer_id: UUID
    conversation_id: UUID


async def make_tenant(db: Database, name: str = "Test Biz") -> Tenant:
    bid, cid, cust, conv = new_id(), new_id(), new_id(), new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO businesses (id, name, slug) VALUES (:id, :n, :slug)"),
            {"id": bid, "n": name, "slug": f"t-{bid}"},
        )
        await s.execute(
            text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                    display_name) VALUES (:id, :b, 'web_widget', 'simulator', :e, 'sim')"""),
            {"id": cid, "b": bid, "e": f"sim-{cid}"},
        )
        await s.execute(
            text("INSERT INTO customers (id, business_id, display_name) VALUES (:id, :b, 'Rahul')"),
            {"id": cust, "b": bid},
        )
        await s.execute(
            text("""INSERT INTO conversations (id, business_id, channel, channel_connection_id,
                                    participant_role, customer_id, thread_key)
                                VALUES (:id, :b, 'web_widget', :c, 'customer', :cust, :tk)"""),
            {"id": conv, "b": bid, "c": cid, "cust": cust, "tk": f"thread-{conv}"},
        )
    return Tenant(bid, cid, cust, conv)


async def make_outbound(db: Database, t: Tenant, parts: int = 1, key: str | None = None) -> tuple[UUID, list[UUID]]:
    """An outbound message with `parts` queued send-log rows (part order = insertion order)."""
    mid = new_id()
    ids: list[UUID] = []
    base = key or str(mid)
    async with db.tenant_session(t.business_id) as s:
        await s.execute(
            text("""INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                          channel_connection_id, content_text, status)
                    VALUES (:id, :b, :c, 'outbound', 'ai', 'web_widget', :cc, 'hello', 'queued')"""),
            {"id": mid, "b": t.business_id, "c": t.conversation_id, "cc": t.connection_id},
        )
        for i in range(parts):
            oid = new_id()
            ids.append(oid)
            await s.execute(
                text("""INSERT INTO outbound_messages (id, business_id, message_id, conversation_id,
                            channel_connection_id, part_index, idempotency_key, rendered_payload, priority)
                        VALUES (:id, :b, :m, :c, :cc, :i, :k, '{}', 'p0')"""),
                {
                    "id": oid,
                    "b": t.business_id,
                    "m": mid,
                    "c": t.conversation_id,
                    "cc": t.connection_id,
                    "i": i,
                    "k": f"{base}:{i}",
                },
            )
    return mid, ids


async def make_inbound_message(db: Database, t: Tenant, text_body: str = "hi") -> UUID:
    mid = new_id()
    async with db.tenant_session(t.business_id) as s:
        await s.execute(
            text("""INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                          channel_connection_id, content_text, status)
                    VALUES (:id, :b, :c, 'inbound', 'customer', 'web_widget', :cc, :txt, 'received')"""),
            {"id": mid, "b": t.business_id, "c": t.conversation_id, "cc": t.connection_id, "txt": text_body},
        )
    return mid
