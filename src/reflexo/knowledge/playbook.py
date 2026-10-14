"""Playbook rules (ADR-015, POC_10 §4): how *this* business wants the agent to act, taught by the owner.

A rule says, for one capability, whether the agent may do it (`allow`), must not (`deny`) or must ask first
(`propose`), optionally only when typed conditions hold. Conditions are data, not code or free CEL: a fixed
catalogue of situation fields, each with a type and a plain-language label, so a rule evaluates deterministically
and reads back to the owner word for word as it will behave. Rules are created, edited and undone through
knowledge change sets (entity type `playbook_rule`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.knowledge.changesets import apply_now

Effect = Literal["allow", "deny", "propose"]
Op = Literal["is", "is_not", "in", "not_in", "lt", "lte", "gt", "gte", "includes"]
SAFETY = {"deny": 3, "propose": 2, "allow": 1}  # on a tie the safest effect wins


@dataclass(frozen=True)
class FieldSpec:
    kind: Literal["enum", "number", "list"]
    en: str
    hi: str
    values: tuple[str, ...] = ()


# The situation a capability is requested in. Every capability fills the fields that apply to it; a condition on
# a missing field is false (a rule never fires on data it cannot see).
FIELDS: dict[str, FieldSpec] = {
    "order.status": FieldSpec(
        "enum", "the order's status", "order ka status",
        ("draft", "awaiting_payment", "confirmed", "preparing", "out_for_delivery", "delivered"),
    ),
    "order.payment_method": FieldSpec("enum", "the payment method", "payment ka tareeka", ("cod", "razorpay_link")),
    "order.payment_status": FieldSpec(
        "enum", "the payment status", "payment status", ("unpaid", "pending", "cod_pending", "paid")
    ),
    "order.total_rupees": FieldSpec("number", "the order total (₹)", "order total (₹)"),
    "order.hours_until_delivery": FieldSpec("number", "hours left until delivery", "delivery mein bache ghante"),
    "order.hours_since_placed": FieldSpec("number", "hours since the order was placed", "order kiye hue ghante"),
    "order.categories": FieldSpec("list", "the order's categories", "order ki categories"),
    "customer.orders_count": FieldSpec("number", "the customer's past orders", "customer ke pichhle orders"),
}  # fmt: skip
OPS_EN = {"is": "is", "is_not": "is not", "in": "is one of", "not_in": "is not one of", "lt": "is below",
          "lte": "is at most", "gt": "is above", "gte": "is at least", "includes": "includes"}  # fmt: skip
OPS_HI = {"is": "hai", "is_not": "nahi hai", "in": "inmein se hai", "not_in": "inmein se nahi hai",
          "lt": "se kam hai", "lte": "zyada se zyada hai", "gt": "se zyada hai", "gte": "kam se kam hai",
          "includes": "mein shaamil hai"}  # fmt: skip


class Condition(BaseModel):
    field: str
    op: Op
    value: str | float | list[str]

    @field_validator("field")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in FIELDS:
            raise ValueError(f"unknown field {v}; use one of {sorted(FIELDS)}")
        return v

    def holds(self, situation: dict[str, Any]) -> bool:
        if self.field not in situation or situation[self.field] is None:
            return False
        have = situation[self.field]
        want = self.value
        if self.op == "includes":
            return str(want) in [str(x) for x in (have or [])]
        if self.op in ("in", "not_in"):
            members = [str(x) for x in (want if isinstance(want, list) else [want])]
            return (str(have) in members) == (self.op == "in")
        if self.op in ("is", "is_not"):
            return (str(have) == str(want)) == (self.op == "is")
        try:
            a, b = float(have), float(want)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[self.op]

    def words(self, lang: str = "en") -> str:
        spec = FIELDS[self.field]
        value = ", ".join(self.value) if isinstance(self.value, list) else str(self.value).replace("_", " ")
        if isinstance(self.value, float) and self.value.is_integer():
            value = str(int(self.value))
        if lang == "en":
            return f"{spec.en} {OPS_EN[self.op]} {value}"
        return f"{spec.hi} {value} {OPS_HI[self.op]}"


class PlaybookRule(BaseModel):
    id: UUID
    capability: str
    effect: Effect
    conditions: list[Condition] = Field(default_factory=list)
    says: str
    scope: Literal["global", "customer"] = "global"
    customer_id: UUID | None = None
    source: Literal["owner_instruction", "owner_decision"]
    source_ref: UUID | None = None

    def applies(self, situation: dict[str, Any]) -> bool:
        return all(c.holds(situation) for c in self.conditions)

    def readback(self, capability_label: str, lang: str = "en") -> str:
        """Deterministic: exactly what the rule will do, in the owner's language."""
        joiner = " and " if lang == "en" else " aur "
        when = joiner.join(c.words(lang) for c in self.conditions)
        if lang == "en":
            verb = {"allow": "I do it myself", "deny": "I say no", "propose": "I ask you first"}[self.effect]
            return f"{capability_label}: {verb}" + (f" when {when}" if when else ", always")
        verb = {"allow": "main khud kar deti hoon", "deny": "main mana kar deti hoon",
                "propose": "main pehle aapse poochti hoon"}[self.effect]  # fmt: skip
        return f"{capability_label}: " + (f"jab {when}, " if when else "hamesha ") + verb


@dataclass(frozen=True)
class RuleMatch:
    rule: PlaybookRule

    @property
    def effect(self) -> Effect:
        return self.rule.effect


def _row(r: Any) -> PlaybookRule:
    return PlaybookRule(
        id=r.id,
        capability=r.capability,
        effect=r.effect,
        conditions=[Condition.model_validate(c) for c in (r.conditions or [])],
        says=r.says,
        scope=str(r.scope),
        customer_id=r.customer_id,
        source=r.source,
        source_ref=r.source_ref,
    )


async def rules(
    business_id: UUID, *, capability: str | None = None, customer_id: UUID | None = None
) -> list[PlaybookRule]:
    """Active rules: global ones plus this customer's (all customers' when `customer_id` is None and the caller
    wants the whole playbook — pass `customer_id` for a decision)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, capability, effect, conditions, says, scope, customer_id, source, source_ref
                        FROM playbook_rules WHERE status = 'active'
                          AND (CAST(:cap AS text) IS NULL OR capability = :cap)
                          AND (scope = 'global' OR CAST(:c AS uuid) IS NULL OR customer_id = CAST(:c AS uuid))
                        ORDER BY created_at"""),
                {"cap": capability, "c": customer_id},
            )
        ).all()
    return [_row(r) for r in rows]


async def match(
    business_id: UUID, capability: str, situation: dict[str, Any], customer_id: UUID | None
) -> RuleMatch | None:
    """The rule that decides this request: this customer's rules before the business-wide ones; within a scope
    the safest effect wins a tie (deny > propose > allow). None = the playbook does not cover it."""
    applicable = [
        r
        for r in await rules(business_id, capability=capability, customer_id=customer_id)
        if (r.scope == "global" or r.customer_id == customer_id) and r.applies(situation)
    ]
    for scope in ("customer", "global"):
        candidates = [r for r in applicable if r.scope == scope]
        if candidates:
            return RuleMatch(max(candidates, key=lambda r: SAFETY[r.effect]))
    return None


async def add_rule(
    business_id: UUID,
    *,
    capability: str,
    effect: Effect,
    conditions: list[Condition],
    says: str,
    source: Literal["owner_instruction", "owner_decision"],
    customer_id: UUID | None = None,
    source_ref: UUID | None = None,
    summary: str = "Playbook rule",
    member_id: UUID | None = None,
) -> UUID:
    """Create an active rule through a change set (so `/undo` reverts it like any other knowledge change)."""
    rule_id = new_id()
    await apply_now(
        business_id,
        "learning",
        summary,
        [
            (
                "playbook_rule",
                "create",
                rule_id,
                {
                    "capability": capability,
                    "effect": effect,
                    "conditions": [c.model_dump() for c in conditions],
                    "says": says,
                    "scope": "customer" if customer_id else "global",
                    "customer_id": str(customer_id) if customer_id else None,
                    "source": source,
                    "source_ref": str(source_ref) if source_ref else None,
                    "status": "active",
                },
            )
        ],
        member_id=member_id,
    )
    return rule_id
