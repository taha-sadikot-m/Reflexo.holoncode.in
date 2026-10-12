"""Approval requests (POC_04 §9): a pending decision with a 4-digit code, decided once by YES/NO or a button.

The module only records decisions; what an approval *does* (apply a change set, send a drafted message) belongs to
the caller that decides it (owner commands), which reads `subject`.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.common.timeutil import utcnow


@dataclass(frozen=True)
class Approval:
    id: UUID
    kind: str
    subject: dict[str, Any]
    preview: dict[str, Any]
    short_code: str
    status: str


@dataclass(frozen=True)
class Decision:
    approval: Approval
    approved: bool
    already_decided: bool = False  # a second YES / a double tap: nothing happens twice
    expired: bool = False


async def create(
    s: AsyncSession,
    business_id: UUID,
    kind: str,
    subject: dict[str, Any],
    preview: dict[str, Any],
    *,
    requested_by: str,
    ttl_min: int = 24 * 60,
) -> Approval:
    """Inside the caller's transaction, so the request exists exactly when the thing it approves does."""
    taken = set(
        (
            await s.execute(
                text("SELECT short_code FROM approval_requests WHERE business_id = :b AND status = 'pending'"),
                {"b": business_id},
            )
        ).scalars()
    )
    code = next(c for c in (f"{secrets.randbelow(9000) + 1000}" for _ in range(100)) if c not in taken)
    aid = new_id()
    await s.execute(
        text("""INSERT INTO approval_requests (id, business_id, kind, subject, preview, short_code, status,
                                               on_expire, expires_at, requested_by)
                VALUES (:id, :b, CAST(:k AS approval_kind), CAST(:s AS jsonb), CAST(:p AS jsonb), :c, 'pending',
                        'reject', :exp, :by)"""),
        {
            "id": aid,
            "b": business_id,
            "k": kind,
            "s": json.dumps(subject, ensure_ascii=False, default=str),
            "p": json.dumps(preview, ensure_ascii=False, default=str),
            "c": code,
            "exp": utcnow() + timedelta(minutes=ttl_min),
            "by": requested_by,
        },
    )
    return Approval(aid, kind, subject, preview, code, "pending")


async def decide(
    business_id: UUID,
    *,
    approve: bool,
    member_id: UUID | None,
    via: str | None,
    approval_id: UUID | None = None,
    code: str | None = None,
) -> Decision | None:
    """Record the owner's decision. None if no such request (wrong code)."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("""SELECT id, kind, subject, preview, short_code, status, expires_at FROM approval_requests
                        WHERE (CAST(:id AS uuid) IS NOT NULL AND id = CAST(:id AS uuid))
                           OR (CAST(:code AS text) IS NOT NULL AND short_code = :code)
                        ORDER BY (status = 'pending') DESC, created_at DESC LIMIT 1 FOR UPDATE"""),
                {"id": approval_id, "code": code},
            )
        ).first()
        if row is None:
            return None
        approval = Approval(
            row.id, str(row.kind), dict(row.subject), dict(row.preview), row.short_code, str(row.status)
        )
        if approval.status != "pending":
            return Decision(approval, approval.status == "approved", already_decided=True)
        if row.expires_at <= utcnow():
            await s.execute(text("UPDATE approval_requests SET status = 'expired' WHERE id = :id"), {"id": row.id})
            return Decision(approval, False, expired=True)
        status = "approved" if approve else "rejected"
        await s.execute(
            text("""UPDATE approval_requests SET status = CAST(:st AS approval_status), decided_by_member_id = :m,
                           decided_via = CAST(:via AS channel_type), decided_at = now(),
                           decision = CAST(:d AS jsonb) WHERE id = :id"""),
            {"st": status, "m": member_id, "via": via, "d": json.dumps({"approve": approve}), "id": row.id},
        )
    return Decision(
        Approval(approval.id, approval.kind, approval.subject, approval.preview, approval.short_code, status), approve
    )


async def pending(business_id: UUID, limit: int = 10) -> list[Approval]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, kind, subject, preview, short_code, status FROM approval_requests
                        WHERE status = 'pending' AND expires_at > now() ORDER BY created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return [Approval(r.id, str(r.kind), dict(r.subject), dict(r.preview), r.short_code, str(r.status)) for r in rows]


def _approval(r: Any) -> Approval:
    return Approval(r.id, str(r.kind), dict(r.subject), dict(r.preview), r.short_code, str(r.status))


async def get(business_id: UUID, approval_id: UUID) -> Approval | None:
    async with get_db().tenant_session(business_id) as s:
        r = (
            await s.execute(
                text("SELECT id, kind, subject, preview, short_code, status FROM approval_requests WHERE id = :id"),
                {"id": approval_id},
            )
        ).first()
    return _approval(r) if r else None


async def record_outcome(business_id: UUID, approval_id: UUID, outcome: dict[str, Any]) -> None:
    """What deciding the request did (the executed action's result) — read by the relay turn and the admin."""
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""UPDATE approval_requests SET decision = COALESCE(decision, '{}'::jsonb) || CAST(:o AS jsonb)
                    WHERE id = :id"""),
            {"o": json.dumps({"outcome": outcome}, default=str), "id": approval_id},
        )


async def outcome(business_id: UUID, approval_id: UUID) -> dict[str, Any] | None:
    async with get_db().tenant_session(business_id) as s:
        d = (
            await s.execute(text("SELECT decision FROM approval_requests WHERE id = :id"), {"id": approval_id})
        ).scalar_one_or_none()
    return dict(d) if d else None


async def open_for(business_id: UUID, *, kind: str, conversation_id: UUID) -> list[Approval]:
    """Pending requests of one kind that concern a conversation (proposals the customer is waiting on)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, kind, subject, preview, short_code, status FROM approval_requests
                        WHERE status = 'pending' AND expires_at > now() AND kind = CAST(:k AS approval_kind)
                          AND subject->>'conversation_id' = :c ORDER BY created_at"""),
                {"k": kind, "c": str(conversation_id)},
            )
        ).all()
    return [_approval(r) for r in rows]
