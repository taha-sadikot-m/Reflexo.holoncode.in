"""Identity resolution, roles, conversations, and verified customer linking (POC_03 §5; specs_v2 D-061/D-064).

* Owner/team only via a verified, unrevoked binding; a `simulated_only` binding is honoured only for signed
  simulated events. On any DB error the sender is treated as a customer (never grant owner rights on error).
* Customers are created race-safely (INSERT … ON CONFLICT DO NOTHING + re-select).
* Cross-channel linking only on verified evidence: a consumed link code, the same WhatsApp user (chat ↔ call),
  or a payment contact matching a WhatsApp-verified phone (POC_05).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import get_settings
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import utcnow
from reflexo.gateway.connections import ConnectionInfo
from reflexo.gateway.normalized import NormalizedEvent

log = get_logger(__name__)

# A person's identity is per surface family: comments and DMs on Instagram share the same IGSID (verify).
IDENTITY_CHANNEL = {"instagram_comment": "instagram_dm"}
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


@dataclass(frozen=True)
class ResolvedSender:
    role: str  # customer | owner | team
    conversation_id: UUID
    customer_id: UUID | None
    member_id: UUID | None
    is_new_customer: bool
    conversation_simulated: bool
    option_map: dict[str, object]


async def _binding(
    s: AsyncSession, business_id: UUID, channel: str, external_id: str, simulated: bool
) -> tuple[UUID, str] | None:
    try:
        row = (
            await s.execute(
                text("""
            SELECT m.id AS member_id, m.role
            FROM owner_channel_bindings b JOIN team_members m ON m.id = b.member_id
            WHERE b.business_id = :b AND b.channel = CAST(:c AS channel_type) AND b.external_id = :e
              AND b.verified_at IS NOT NULL AND b.revoked_at IS NULL AND m.status = 'active'
              AND (NOT b.simulated_only OR :sim)
            LIMIT 1"""),
                {"b": business_id, "c": channel, "e": external_id, "sim": simulated},
            )
        ).first()
    except SQLAlchemyError:
        log.exception("binding_lookup_failed_treating_as_customer", business_id=str(business_id))
        return None
    return (row.member_id, str(row.role)) if row else None


async def _conversation(
    s: AsyncSession,
    conn: ConnectionInfo,
    thread_key: str,
    role: str,
    customer_id: UUID | None,
    member_id: UUID | None,
    simulated: bool,
) -> tuple[UUID, bool, dict[str, Any]]:
    row = (
        await s.execute(
            text("""
        INSERT INTO conversations (id, business_id, channel, channel_connection_id, participant_role, customer_id,
                                   member_id, thread_key, simulated)
        VALUES (:id, :b, CAST(:ch AS channel_type), :cc, CAST(:role AS participant_role), :cust, :mem, :tk, :sim)
        ON CONFLICT (channel_connection_id, thread_key) DO UPDATE SET updated_at = now()
        RETURNING id, simulated, option_map"""),
            {
                "id": new_id(),
                "b": conn.business_id,
                "ch": conn.channel,
                "cc": conn.id,
                "role": role,
                "cust": customer_id,
                "mem": member_id,
                "tk": thread_key,
                "sim": simulated,
            },
        )
    ).one()
    return row.id, bool(row.simulated), dict(row.option_map or {})


async def resolve_customer(
    s: AsyncSession,
    business_id: UUID,
    channel: str,
    external_id: str,
    display_name: str | None,
    connection_id: UUID | None,
) -> tuple[UUID, bool]:
    """Find or create the customer for (business, channel family, external id). Returns (customer_id, created)."""
    ident_channel = IDENTITY_CHANNEL.get(channel, channel)
    found = (
        await s.execute(
            text("""
        SELECT customer_id FROM identity_map WHERE business_id = :b AND channel = CAST(:c AS channel_type)
          AND external_id = :e"""),
            {"b": business_id, "c": ident_channel, "e": external_id},
        )
    ).scalar_one_or_none()
    if found is not None:
        await s.execute(
            text("""UPDATE identity_map SET last_inbound_at = now(),
                                   display_name = COALESCE(:n, display_name)
                                WHERE business_id = :b AND channel = CAST(:c AS channel_type) AND external_id = :e"""),
            {"n": display_name, "b": business_id, "c": ident_channel, "e": external_id},
        )
        await s.execute(text("UPDATE customers SET last_seen_at = now() WHERE id = :id"), {"id": found})
        return found, False
    customer_id = new_id()
    phone = external_id if ident_channel == "whatsapp" else None  # WhatsApp proves possession of the number
    await s.execute(
        text("""INSERT INTO customers (id, business_id, display_name, phone_e164)
                            VALUES (:id, :b, :n, :p)"""),
        {"id": customer_id, "b": business_id, "n": display_name, "p": phone},
    )
    inserted = (
        await s.execute(
            text("""
        INSERT INTO identity_map (id, business_id, channel, external_id, customer_id, channel_connection_id,
                                  display_name, last_inbound_at)
        VALUES (:id, :b, CAST(:c AS channel_type), :e, :cust, :cc, :n, now())
        ON CONFLICT (business_id, channel, external_id) DO NOTHING
        RETURNING customer_id"""),
            {
                "id": new_id(),
                "b": business_id,
                "c": ident_channel,
                "e": external_id,
                "cust": customer_id,
                "cc": connection_id,
                "n": display_name,
            },
        )
    ).scalar_one_or_none()
    if inserted is None:  # lost a race with a concurrent first message: use the winner, drop our customer row
        await s.execute(text("DELETE FROM customers WHERE id = :id"), {"id": customer_id})
        winner = (
            await s.execute(
                text("""
            SELECT customer_id FROM identity_map WHERE business_id = :b AND channel = CAST(:c AS channel_type)
              AND external_id = :e"""),
                {"b": business_id, "c": ident_channel, "e": external_id},
            )
        ).scalar_one()
        return winner, False
    return customer_id, True


async def resolve(s: AsyncSession, conn: ConnectionInfo, ev: NormalizedEvent, simulated: bool) -> ResolvedSender:
    sender = ev.sender_external_id or ev.thread_key
    if {"owner", "team"} & set(conn.roles) and sender:
        hit = await _binding(s, conn.business_id, conn.channel, sender, simulated)
        if hit is not None:
            member_id, role = hit
            participant = "owner" if role == "owner" else "team"
            conv_id, conv_sim, options = await _conversation(
                s, conn, ev.thread_key, participant, None, member_id, simulated
            )
            return ResolvedSender(participant, conv_id, None, member_id, False, conv_sim, options)
    customer_id, created = await resolve_customer(
        s, conn.business_id, ev.channel, sender, ev.sender_display_name, conn.id
    )
    conv_id, conv_sim, options = await _conversation(s, conn, ev.thread_key, "customer", customer_id, None, simulated)
    return ResolvedSender("customer", conv_id, customer_id, None, created, conv_sim, options)


# ---------------------------------------------------------------- verified linking (F13)


def new_link_code() -> str:
    return "LINK-" + "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(5))


async def issue_link_code(
    s: AsyncSession, business_id: UUID, customer_id: UUID, source_channel: str
) -> tuple[str, str]:
    """Create a one-time code and the wa.me deep link that carries it. Returns (code, url)."""
    code = new_link_code()
    ttl = get_settings().link_code_ttl_min
    await s.execute(
        text("""
        INSERT INTO customer_link_codes (id, business_id, code, source_customer_id, source_channel, target_channel,
                                         expires_at)
        VALUES (:id, :b, :code, :c, CAST(:src AS channel_type), 'whatsapp', :exp)"""),
        {
            "id": new_id(),
            "b": business_id,
            "code": code,
            "c": customer_id,
            "src": source_channel,
            "exp": utcnow() + timedelta(minutes=ttl),
        },
    )
    number = get_settings().wa_display_number.lstrip("+")
    return code, f"https://wa.me/{number}?text={code}"


@dataclass(frozen=True)
class LinkOutcome:
    status: str  # linked | already_linked | expired | unknown
    customer_id: UUID | None = None
    merged_customer_id: UUID | None = None


async def merge_customers(s: AsyncSession, business_id: UUID, keep: UUID, drop: UUID) -> None:
    """Move everything from `drop` to `keep` (identities, conversations, orders, links, Q&A), union memory."""
    for table in ("identity_map", "conversations", "orders", "payment_links", "call_sessions"):
        await s.execute(
            text(f"UPDATE {table} SET customer_id = :keep WHERE customer_id = :drop AND business_id = :b"),  # noqa: S608 - fixed table list
            {"keep": keep, "drop": drop, "b": business_id},
        )
    await s.execute(
        text("""UPDATE qa_entries SET customer_id = :keep WHERE customer_id = :drop AND business_id = :b"""),
        {"keep": keep, "drop": drop, "b": business_id},
    )
    await s.execute(
        text("""
        UPDATE customers k SET
          memory = jsonb_build_object('facts',
                     COALESCE(k.memory->'facts', '[]'::jsonb) || COALESCE(d.memory->'facts', '[]'::jsonb)),
          tags = ARRAY(SELECT DISTINCT unnest(k.tags || d.tags)),
          phone_e164 = COALESCE(k.phone_e164, d.phone_e164),
          display_name = COALESCE(k.display_name, d.display_name),
          first_seen_at = LEAST(k.first_seen_at, d.first_seen_at)
        FROM customers d WHERE k.id = :keep AND d.id = :drop"""),
        {"keep": keep, "drop": drop},
    )
    await s.execute(text("UPDATE customers SET merged_into = :keep WHERE id = :drop"), {"keep": keep, "drop": drop})


async def consume_link_code(s: AsyncSession, business_id: UUID, code: str, wa_customer_id: UUID) -> LinkOutcome:
    """The WhatsApp sender of `code` is the same person as the code's source customer (possession proof)."""
    row = (
        await s.execute(
            text("""
        SELECT id, source_customer_id, status, expires_at FROM customer_link_codes
        WHERE business_id = :b AND code = :code FOR UPDATE"""),
            {"b": business_id, "code": code.upper()},
        )
    ).first()
    if row is None:
        return LinkOutcome("unknown")
    if row.status != "issued" or row.expires_at < utcnow():
        if row.status == "issued":
            await s.execute(text("UPDATE customer_link_codes SET status = 'expired' WHERE id = :id"), {"id": row.id})
        return LinkOutcome("expired")
    source = row.source_customer_id
    await s.execute(
        text("""UPDATE customer_link_codes SET status = 'consumed', consumed_at = now() WHERE id = :id"""),
        {"id": row.id},
    )
    if source == wa_customer_id:
        return LinkOutcome("already_linked", source)
    await merge_customers(s, business_id, keep=source, drop=wa_customer_id)
    await s.execute(
        text("""UPDATE identity_map SET linked_via = 'link_code'
                            WHERE business_id = :b AND customer_id = :c AND channel = 'whatsapp'"""),
        {"b": business_id, "c": source},
    )
    log.info("customer_linked", business_id=str(business_id), kept=str(source), merged=str(wa_customer_id))
    return LinkOutcome("linked", source, wa_customer_id)


async def record_opt(s: AsyncSession, customer_id: UUID, channel: str, opted_out: bool) -> None:
    """Opt-out/in as a customer memory fact (POC_03 §9.2)."""
    key = f"opted_out:{channel}"
    fact = {"key": key, "value": opted_out, "source": "customer", "at": utcnow().isoformat()}
    await s.execute(
        text("""
        UPDATE customers SET memory = jsonb_build_object('facts',
            COALESCE((SELECT jsonb_agg(f) FROM jsonb_array_elements(COALESCE(memory->'facts', '[]'::jsonb)) f
                      WHERE f->>'key' <> :k), '[]'::jsonb) || CAST(:fact AS jsonb))
        WHERE id = :id"""),
        {"k": key, "fact": json.dumps([fact]), "id": customer_id},
    )
