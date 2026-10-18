"""Operator access to a business's customer data is logged (BETA_10 §5.2 T15, §13; CHK-SEC-11).

Every operator or reviewer view of a business's conversations, customers, orders or traces writes an
`operator_access_log` row, which the business's owner can see on request. A console that polls the same view
writes one row per view per 10 minutes, not one per poll. Owners looking at their own shop are not logged.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id

DEDUPE_S = 600
PAGE = 100
LogKind = Literal["operator", "credential"]
# the two append-only logs the Access logs page reads (BETA_13 §3): who looked at customer data, who read a credential
_LOGS: dict[str, str] = {
    "operator": """SELECT l.created_at, l.business_id, b.name AS business, l.operator AS who, l.object_type AS what,
                         l.object_id, l.reason AS detail, true AS ok
                  FROM operator_access_log l JOIN businesses b ON b.id = l.business_id""",
    "credential": """SELECT l.created_at, l.business_id, b.name AS business, l.accessor AS who, v.kind::text AS what,
                           l.credential_id AS object_id, l.purpose AS detail, l.ok
                    FROM credential_access_log l JOIN businesses b ON b.id = l.business_id
                    JOIN credential_vault v ON v.id = l.credential_id""",
}
_recent: dict[tuple[str, UUID, str, str | None], float] = {}


async def log_access(business_id: UUID, operator: str, object_type: str, object_id: UUID | None, reason: str) -> bool:
    """Write the row unless the same view was logged in the last 10 minutes; True when written."""
    key = (operator, business_id, object_type, str(object_id) if object_id else None)
    now = time.monotonic()
    if now - _recent.get(key, -DEDUPE_S) < DEDUPE_S:
        return False
    _recent[key] = now
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO operator_access_log (id, business_id, operator, object_type, object_id, reason)
                    VALUES (:id, :b, :op, :t, :o, :r)"""),
            {"id": new_id(), "b": business_id, "op": operator, "t": object_type, "o": object_id, "r": reason},
        )
    return True


def forget() -> None:
    _recent.clear()


async def entries(
    kind: LogKind, *, businesses: list[UUID] | None = None, before: datetime | None = None, limit: int = PAGE
) -> list[dict[str, Any]]:
    """The newest rows of one access log (`businesses` None: all), older pages by `before`. Operators only."""
    sql = (
        _LOGS[kind]
        + """ WHERE (CAST(:b AS uuid[]) IS NULL OR l.business_id = ANY(CAST(:b AS uuid[])))
               AND (CAST(:before AS timestamptz) IS NULL OR l.created_at < CAST(:before AS timestamptz))
             ORDER BY l.created_at DESC LIMIT :n"""
    )
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (await s.execute(text(sql), {"b": businesses, "before": before, "n": limit})).all()
    return [
        {
            "at": r.created_at.isoformat(),
            "business_id": str(r.business_id),
            "business": r.business,
            "who": r.who,
            "what": r.what,
            "object_id": str(r.object_id) if r.object_id else None,
            "detail": r.detail,
            "ok": bool(r.ok),
        }
        for r in rows
    ]
