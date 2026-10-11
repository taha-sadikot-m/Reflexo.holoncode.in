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
