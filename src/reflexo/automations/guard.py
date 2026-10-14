"""Order guards (OrderGuardPort, POC_06 §6): capacity rules checked and reserved atomically at order confirm.

* `counter_limit` reserves `amount` on `automation_counters` with one conditional UPDATE (`value + amount <= limit`),
  so concurrent confirms can never oversell; a claim per (order, counter) makes retries of the same order free.
* Exhausted → deny with the guard's reason and the next dates that still have room.
* Any error fails closed (deny, owner notified); `release` gives an order's reservations back.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.knowledge.api as knowledge
from reflexo.automations import cel
from reflexo.automations.schema import AutomationDefinition, CounterLimitStep, DenyStep
from reflexo.automations.store import active_versions
from reflexo.automations.variables import customer_variables, order_variables
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import GuardResult, OwnerNotifyPort
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.language import fixed
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)


def fail_closed() -> str:
    return fixed(
        "I can't confirm that right now — I'll try again in a minute 🙏",
        "Abhi confirm nahi kar pa rahi, ek minute mein try karti hoon 🙏",
    )


ALTERNATIVES = 5
LOOKAHEAD_DAYS = 30


def _claim_key(order_id: str, amount: int, counter_key: str) -> str:
    return f"order:{order_id}:{amount}:{counter_key}"


async def _reserve(
    s: AsyncSession, business_id: UUID, automation_id: UUID, key: str, amount: int, limit: int, order_id: str
) -> bool:
    """True if reserved (or already reserved by this order). One statement decides — safe under concurrency."""
    claim = _claim_key(order_id, amount, key)
    existing = (
        await s.execute(
            text("SELECT 1 FROM automation_claims WHERE automation_id = :a AND claim_key = :k"),
            {"a": automation_id, "k": claim},
        )
    ).first()
    if existing is not None:
        return True
    await s.execute(
        text("""INSERT INTO automation_counters (automation_id, business_id, counter_key, value, limit_value)
                VALUES (:a, :b, :k, 0, :lim) ON CONFLICT (automation_id, counter_key) DO NOTHING"""),
        {"a": automation_id, "b": business_id, "k": key, "lim": limit},
    )
    reserved = (
        await s.execute(
            text("""UPDATE automation_counters SET value = value + :amt, limit_value = :lim
                    WHERE automation_id = :a AND counter_key = :k AND value + :amt <= :lim RETURNING value"""),
            {"amt": amount, "lim": limit, "a": automation_id, "k": key},
        )
    ).first()
    if reserved is None:
        return False
    await s.execute(
        text("""INSERT INTO automation_claims (automation_id, business_id, claim_key, run_id)
                VALUES (:a, :b, :k, CAST(:o AS uuid))"""),
        {"a": automation_id, "b": business_id, "k": claim, "o": order_id},
    )
    return True


class AutomationGuard:
    """OrderGuardPort."""

    async def check(self, business_id: UUID, order: dict[str, Any]) -> GuardResult:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._check(business_id, order), get_settings().guard_timeout_ms / 1000)
        except Exception as exc:  # fail closed: never confirm an order we could not check
            log.exception("guard_failed", business_id=str(business_id), error=type(exc).__name__)
            await self._tell_owner(business_id, order)
            return GuardResult(False, fail_closed(), [], code="GUARD_ERROR")
        await emit(
            "guard",
            PipelineEventStatus.OK if result.allowed else PipelineEventStatus.SKIP,
            business_id=business_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            allowed=result.allowed,
            code=result.code,
        )
        return result

    async def _check(self, business_id: UUID, order: dict[str, Any]) -> GuardResult:
        guards = await active_versions(business_id, executor="guard")
        if not guards:
            return GuardResult(True)
        settings = await knowledge.business_settings(business_id)
        tz = str(settings.get("timezone") or "Asia/Kolkata")
        variables = {
            "order": await order_variables(business_id, order),
            "customer": await customer_variables(
                business_id, UUID(order["customer_id"]) if order.get("customer_id") else None
            ),
            "business": {"name": settings.get("name")},
            "event": {"type": "order_confirming"},
            "vars": {},
            "steps": {},
        }
        order_id = variables["order"]["id"]
        reserved: list[tuple[UUID, str, int]] = []
        async with get_db().tenant_session(business_id) as s:
            for g in guards:
                d = g.definition
                if d.trigger.cel and not cel.evaluate(d.trigger.cel, variables, tz=tz):
                    continue
                step: Any = d.steps[0]
                while step is not None and step.type != "end":
                    if step.type == "branch":
                        step = d.step(step.then if cel.evaluate(step.cel, variables, tz=tz) else step.else_)
                        continue
                    if isinstance(step, DenyStep):
                        await s.rollback()
                        return await self._deny(d, step, None, variables, tz, business_id, g.automation_id)
                    assert isinstance(step, CounterLimitStep)
                    key = cel.render(step.key, variables, tz=tz)
                    amount = int(cel.evaluate(step.amount, variables, tz=tz))
                    if amount <= 0:
                        step = d.step(step.next) if step.next else None
                        continue
                    if await _reserve(s, business_id, g.automation_id, key, amount, step.limit, order_id):
                        reserved.append((g.automation_id, key, amount))
                        step = d.step(step.next) if step.next else None
                    elif step.on_exhausted == "skip":
                        step = d.step(step.next) if step.next else None
                    elif step.on_exhausted == "goto" and step.goto:
                        step = d.step(step.goto)
                    else:
                        deny = next(x for x in d.steps if isinstance(x, DenyStep))
                        await s.rollback()  # nothing from this check stays reserved
                        return await self._deny(d, deny, step, variables, tz, business_id, g.automation_id)
        return GuardResult(True)

    async def _deny(
        self,
        d: AutomationDefinition,
        deny: DenyStep,
        counter: CounterLimitStep | None,
        variables: dict[str, Any],
        tz: str,
        business_id: UUID,
        automation_id: UUID,
    ) -> GuardResult:
        reason = cel.render(deny.reason, variables, tz=tz)
        alternatives: list[str] = []
        if deny.alternatives == "next_available_dates" and counter is not None:
            alternatives = await self.next_available_dates(business_id, automation_id, counter, variables, tz)
        log.info("guard_denied", business_id=str(business_id), automation=d.name, alternatives=alternatives)
        return GuardResult(False, reason, alternatives, code="CAPACITY_FULL")

    async def next_available_dates(
        self,
        business_id: UUID,
        automation_id: UUID,
        counter: CounterLimitStep,
        variables: dict[str, Any],
        tz: str,
    ) -> list[str]:
        """The next dates (from the earliest deliverable one) where this order still fits."""
        amount = int(cel.evaluate(counter.amount, variables, tz=tz))
        earliest = await knowledge.earliest_delivery_date(business_id)
        candidates: dict[str, str] = {}
        for i in range(LOOKAHEAD_DAYS):
            day = (earliest + timedelta(days=i)).isoformat()
            shifted = {**variables, "order": {**variables["order"], "delivery_date": day}}
            candidates[cel.render(counter.key, shifted, tz=tz)] = day
        async with get_db().tenant_session(business_id) as s:
            used = {
                r.counter_key: int(r.value)
                for r in (
                    await s.execute(
                        text("""SELECT counter_key, value FROM automation_counters
                                WHERE automation_id = :a AND counter_key = ANY(:keys)"""),
                        {"a": automation_id, "keys": list(candidates)},
                    )
                ).all()
            }
        out = [day for key, day in candidates.items() if used.get(key, 0) + amount <= counter.limit]
        return [date.fromisoformat(d).isoformat() for d in out[:ALTERNATIVES]]

    async def release(self, business_id: UUID, order_id: UUID, reason: str) -> None:
        """Give back everything this order reserved (cancelled, payment link expired, COD declined)."""
        async with get_db().tenant_session(business_id) as s:
            claims = (
                await s.execute(
                    text("""DELETE FROM automation_claims WHERE claim_key LIKE :p
                            RETURNING automation_id, claim_key"""),
                    {"p": f"order:{order_id}:%"},
                )
            ).all()
            for c in claims:
                _, _, amount, counter_key = c.claim_key.split(":", 3)
                await s.execute(
                    text("""UPDATE automation_counters SET value = GREATEST(value - :amt, 0)
                            WHERE automation_id = :a AND counter_key = :k"""),
                    {"amt": int(amount), "a": c.automation_id, "k": counter_key},
                )
        if claims:
            log.info(
                "guard_released",
                business_id=str(business_id),
                order_id=str(order_id),
                reason=reason,
                counters=len(claims),
            )

    async def _tell_owner(self, business_id: UUID, order: dict[str, Any]) -> None:
        notifier = ports.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
        if notifier is None:
            return
        await notifier.notify(
            business_id,
            "guard_error",
            f"⚠️ Order {order.get('order_number')} could not be checked against your limits; the customer was asked "
            "to wait a minute.",
            dedupe_key=f"guard_error:{order.get('order_id')}",
            priority=PriorityClass.P1,
            about_conversation=UUID(str(order["conversation_id"])) if order.get("conversation_id") else None,
        )
