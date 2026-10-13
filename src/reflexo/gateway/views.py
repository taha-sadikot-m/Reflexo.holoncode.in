"""Read models of conversations, customers and messages for the desks (POC_04 §4.2 `prepare`).

The gateway owns these tables; the agent reads them only through these functions (`gateway.api`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.timeutil import utcnow
from reflexo.gateway.manifests import manifest_for


@dataclass(frozen=True)
class CustomerView:
    id: UUID
    display_name: str | None
    phone_e164: str | None
    language: str | None
    facts: list[dict[str, Any]]
    summary: str | None


@dataclass(frozen=True)
class ConversationView:
    id: UUID
    business_id: UUID
    channel: str
    provider: str
    connection_id: UUID
    participant_role: str
    customer: CustomerView | None
    member_id: UUID | None
    summary: str | None
    job_description: str | None
    simulated: bool
    capabilities: str


@dataclass(frozen=True)
class MessageView:
    id: UUID
    direction: str
    author: str
    text: str
    created_at: datetime
    interactive_option: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)  # {kind, mime, description}


def capability_summary(provider: str) -> str:
    m = manifest_for(provider)
    parts = [
        f"max text {m.max_text} chars",
        f"buttons: {m.max_buttons}" if m.max_buttons else "buttons: no (quick replies instead)",
        f"lists: up to {m.list_max_rows} rows" if m.list_supported else "lists: no",
        f"carousel: up to {m.carousel_max}" if m.carousel_supported else "carousel: no",
        "product photos with captions" if m.caption_max else "product photos without captions",
    ]
    return ", ".join(parts)


async def conversation_view(s: AsyncSession, conversation_id: UUID) -> ConversationView | None:
    row = (
        await s.execute(
            text("""
        SELECT c.id, c.business_id, c.channel, c.channel_connection_id, c.participant_role, c.member_id,
               c.summary, c.simulated, cc.provider, cc.job_description,
               cu.id AS cust_id, cu.display_name, cu.phone_e164, cu.language, cu.memory, cu.summary AS cust_summary
        FROM conversations c JOIN channel_connections cc ON cc.id = c.channel_connection_id
        LEFT JOIN customers cu ON cu.id = c.customer_id
        WHERE c.id = :id"""),
            {"id": conversation_id},
        )
    ).first()
    if row is None:
        return None
    customer = None
    if row.cust_id is not None:
        memory = row.memory or {}
        customer = CustomerView(
            id=row.cust_id,
            display_name=row.display_name,
            phone_e164=row.phone_e164,
            language=row.language,
            facts=[f for f in memory.get("facts", []) if not str(f.get("key", "")).startswith("opted_out:")],
            summary=row.cust_summary,
        )
    return ConversationView(
        id=row.id,
        business_id=row.business_id,
        channel=str(row.channel),
        provider=str(row.provider),
        connection_id=row.channel_connection_id,
        participant_role=str(row.participant_role),
        customer=customer,
        member_id=row.member_id,
        summary=row.summary,
        job_description=row.job_description,
        simulated=bool(row.simulated),
        capabilities=capability_summary(str(row.provider)),
    )


async def _attachments(s: AsyncSession, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [a.get("media_object_id") for a in raw if a.get("media_object_id")]
    descriptions: dict[str, str | None] = {}
    if ids:
        rows = (
            await s.execute(
                text("SELECT id, description FROM media_objects WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
            )
        ).all()
        descriptions = {str(r.id): r.description for r in rows}
    return [
        {"kind": a.get("kind"), "mime": a.get("mime"), "description": descriptions.get(str(a.get("media_object_id")))}
        for a in raw
    ]


async def _messages(s: AsyncSession, where: str, params: dict[str, Any]) -> list[MessageView]:
    rows = (
        await s.execute(
            text(f"""SELECT id, direction, author, content_text, created_at, interactive_reply, attachments
                     FROM messages WHERE {where}"""),  # noqa: S608 - fixed clauses below
            params,
        )
    ).all()
    out = []
    for r in rows:
        out.append(
            MessageView(
                id=r.id,
                direction=str(r.direction),
                author=str(r.author),
                text=r.content_text or "",
                created_at=r.created_at,
                interactive_option=(r.interactive_reply or {}).get("option_id"),
                attachments=await _attachments(s, r.attachments or []),
            )
        )
    return out


async def recent_messages(
    s: AsyncSession, conversation_id: UUID, *, limit: int = 20, exclude: list[UUID] | None = None
) -> list[MessageView]:
    """Last `limit` sent/received messages, oldest first (blocked/failed drafts excluded)."""
    msgs = await _messages(
        s,
        """conversation_id = :c AND NOT (id = ANY(CAST(:ex AS uuid[])))
           AND status NOT IN ('blocked', 'failed', 'pending_approval')
           ORDER BY created_at DESC, id DESC LIMIT :n""",
        {"c": conversation_id, "ex": [str(x) for x in exclude or []], "n": limit},
    )
    return list(reversed(msgs))


async def messages_by_ids(s: AsyncSession, ids: list[UUID]) -> list[MessageView]:
    return await _messages(s, "id = ANY(CAST(:ids AS uuid[])) ORDER BY created_at, id", {"ids": [str(i) for i in ids]})


async def add_customer_facts(s: AsyncSession, customer_id: UUID, facts: list[dict[str, str]], source: str) -> None:
    """Upsert memory facts by key (latest wins), keeping provenance."""
    if not facts:
        return
    now = utcnow().isoformat()
    new = [{"key": f["key"], "value": f["value"], "source": source, "at": now} for f in facts]
    keys = [f["key"] for f in facts]
    await s.execute(
        text("""
        UPDATE customers SET memory = jsonb_build_object('facts',
            COALESCE((SELECT jsonb_agg(f) FROM jsonb_array_elements(COALESCE(memory->'facts', '[]'::jsonb)) f
                      WHERE NOT (f->>'key' = ANY(CAST(:keys AS text[])))), '[]'::jsonb) || CAST(:new AS jsonb)),
            updated_at = now()
        WHERE id = :id"""),
        {"keys": keys, "new": json.dumps(new, ensure_ascii=False), "id": customer_id},
    )


async def set_customer_language(s: AsyncSession, customer_id: UUID, language: str) -> None:
    await s.execute(text("UPDATE customers SET language = :l WHERE id = :id"), {"l": language, "id": customer_id})


async def mark_needs_attention(s: AsyncSession, conversation_id: UUID) -> None:
    await s.execute(
        text("UPDATE conversations SET needs_attention = true, updated_at = now() WHERE id = :id"),
        {"id": conversation_id},
    )


async def owner_timeline(
    s: AsyncSession, member_id: UUID, *, limit: int = 30, exclude: list[UUID] | None = None
) -> list[tuple[str, MessageView]]:
    """One owner timeline (POC_04 §8): the member's last messages across all their owner conversations, oldest
    first, each tagged with its channel — a file sent on WhatsApp is visible to a command on Telegram."""
    rows = (
        await s.execute(
            text("""SELECT m.id, m.direction, m.author, m.content_text, m.created_at, m.interactive_reply,
                           m.attachments, c.channel
                    FROM messages m JOIN conversations c ON c.id = m.conversation_id
                    WHERE c.member_id = :mem AND c.participant_role = 'owner'
                      AND NOT (m.id = ANY(CAST(:ex AS uuid[])))
                      AND m.status NOT IN ('blocked', 'failed', 'pending_approval')
                    ORDER BY m.created_at DESC, m.id DESC LIMIT :n"""),
            {"mem": member_id, "ex": [str(x) for x in exclude or []], "n": limit},
        )
    ).all()
    out = [
        (
            str(r.channel),
            MessageView(
                id=r.id,
                direction=str(r.direction),
                author=str(r.author),
                text=r.content_text or "",
                created_at=r.created_at,
                interactive_option=(r.interactive_reply or {}).get("option_id"),
                attachments=await _attachments(s, r.attachments or []),
            ),
        )
        for r in rows
    ]
    return list(reversed(out))


async def recent_customer_media(
    s: AsyncSession, conversation_id: UUID, *, within_min: int = 120, burst_min: int = 10, limit: int = 3
) -> list[UUID]:
    """The images that belong to what the customer is asking about now, newest first: their latest image (within
    `within_min`) and the ones sent together with it (up to `burst_min` before it). A photo from an earlier,
    separate request is not part of it (live 14 Oct: the owner got the design from a previous call as well)."""
    rows = (
        await s.execute(
            text("""WITH imgs AS (
                        SELECT m.created_at, a->>'media_object_id' AS mid
                        FROM messages m, jsonb_array_elements(m.attachments) a
                        WHERE m.conversation_id = :c AND m.direction = 'inbound'
                          AND m.created_at > now() - make_interval(mins => :w)
                          AND a->>'kind' = 'image' AND a->>'media_object_id' IS NOT NULL)
                    SELECT mid FROM imgs
                    WHERE created_at >= (SELECT max(created_at) FROM imgs) - make_interval(mins => :burst)
                    ORDER BY created_at DESC LIMIT :n"""),
            {"c": conversation_id, "w": within_min, "burst": burst_min, "n": limit},
        )
    ).all()
    return [UUID(r.mid) for r in rows]
