"""What the agent must know about the owner's procedures (ADR-015 awareness, POC_10 §7).

The customer desk never contradicts the shop: it sees every active automation in plain words (the same
deterministic read-back the owner approved) and what a running automation is waiting for from this customer —
e.g. the COD reconfirmation asked them to reply YES, so the agent says "please reply YES" instead of "confirmed".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from reflexo.automations.readback import describe
from reflexo.automations.store import active_versions
from reflexo.common.db import get_db

MAX_PROCEDURES = 10


class ShopProcedures:
    """ShopProceduresPort."""

    async def describe(self, business_id: UUID) -> list[str]:
        versions = await active_versions(business_id)
        return [f"“{v.name}”: {describe(v.definition, 'en')}" for v in versions[:MAX_PROCEDURES]]

    async def waiting_on(self, business_id: UUID, customer_id: UUID | None) -> list[str]:
        if customer_id is None:
            return []
        async with get_db().tenant_session(business_id) as s:
            rows = (
                await s.execute(
                    text("""SELECT a.name, w.expires_at - now() AS left_for FROM automation_waits w
                            JOIN automation_runs r ON r.id = w.run_id
                            JOIN automation_versions v ON v.id = r.automation_version_id
                            JOIN automations a ON a.id = v.automation_id
                            WHERE w.status = 'waiting' AND w.event_type = 'message_received'
                              AND w.correlation_key = :c AND w.expires_at > now()
                            ORDER BY w.created_at"""),
                    {"c": str(customer_id)},
                )
            ).all()
        return [
            f"The shop's rule “{r.name}” is waiting for this customer's reply to the message it sent above "
            f"(about {max(1, round(r.left_for.total_seconds() / 60))} min left); their reply goes to that rule. "
            "Never contradict it: if they ask, point them to that message."
            for r in rows
        ]
