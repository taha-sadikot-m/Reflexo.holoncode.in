"""Operator access to a business's customer data is logged (BETA_10 §5.2 T15, §13; CHK-SEC-11).

Every operator or reviewer view of a business's conversations, customers, orders or traces writes an
`operator_access_log` row, which the business's owner can see on request. A console that polls the same view
writes one row per view per 10 minutes, not one per poll. Owners looking at their own shop are not logged.
"""

from __future__ import annotations

import time
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import get_db
from reflexo.common.ids import new_id

DEDUPE_S = 600
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
