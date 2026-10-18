"""The closed rule language of the playbook (ADR-015, POC_10 §4; BETA_04 §7.4).

A rule says, for one capability, whether the agent may do it (`allow`), must not (`deny`) or must ask first
(`propose`), optionally only when typed conditions hold. Conditions are data, not code: a fixed catalogue of
situation fields, each with a type, a plain-language label and where it lives in a Cedar request. So a rule
compiles to Cedar by direct translation, evaluates the same in the reference evaluator, and reads back to the
owner word for word as it will behave.

Numbers are whole (rupees, hours, counts), as in the Cedar schema: a situation's value is floored before it is
decided, and the compiler turns a fractional threshold into the equivalent whole-number comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from reflexo.common.config import get_settings

RuleEffect = Literal["allow", "deny", "propose"]
Op = Literal["is", "is_not", "in", "not_in", "lt", "lte", "gt", "gte", "includes"]
SAFETY = {"deny": 3, "propose": 2, "allow": 1}  # on a tie the safest effect wins
MAX_NUMBER = 10**12  # far inside Cedar's 64-bit Long
OPS_BY_KIND: dict[str, tuple[str, ...]] = {
    "enum": ("is", "is_not", "in", "not_in"),
    "number": ("is", "is_not", "lt", "lte", "gt", "gte"),
    "list": ("includes",),
}


@dataclass(frozen=True)
class FieldSpec:
    kind: Literal["enum", "number", "list"]
    on: Literal["order", "customer", "context"]  # where the value lives in a Cedar request
    attr: str  # the attribute (order, customer) or context field name
    values: tuple[str, ...] = ()
    name: str = ""

    @property
    def en(self) -> str:
        return str(_labels()[self.name]["en"])

    @property
    def hi(self) -> str:
        return str(_labels()[self.name]["hi"])


@lru_cache(maxsize=1)
def _labels() -> dict[str, dict[str, str]]:
    """How the owner reads each field (`config/policy/fields.yaml`)."""
    path = Path(get_settings().policy_dir) / "fields.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _fields(**specs: FieldSpec) -> dict[str, FieldSpec]:
    return {key.replace("__", "."): replace(spec, name=key.replace("__", ".")) for key, spec in specs.items()}


# The situation a capability is requested in. Every capability fills the fields that apply to it; a condition on
# a missing field is false (a rule never fires on data it cannot see).
FIELDS: dict[str, FieldSpec] = _fields(
    order__status=FieldSpec(
        "enum", "order", "status",
        ("draft", "awaiting_payment", "confirmed", "preparing", "out_for_delivery", "delivered"),
    ),
    order__payment_method=FieldSpec("enum", "order", "payment_method", ("cod", "razorpay_link")),
    order__payment_status=FieldSpec("enum", "order", "payment_status", ("unpaid", "pending", "cod_pending", "paid")),
    order__total_rupees=FieldSpec("number", "order", "total_rupees"),
    order__hours_until_delivery=FieldSpec("number", "context", "hours_until_delivery"),
    order__hours_since_placed=FieldSpec("number", "context", "hours_since_placed"),
    order__categories=FieldSpec("list", "order", "categories"),
    customer__orders_count=FieldSpec("number", "customer", "orders_count"),
    # limits over history (BETA_04 §7.7): computed before deciding, counting the requested action as done
    refunds__today_rupees=FieldSpec("number", "context", "refunds_today_rupees"),
    cancellations__this_week=FieldSpec("number", "context", "cancellations_this_week"),
)  # fmt: skip
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

    @model_validator(mode="after")
    def _typed(self) -> Condition:
        """The op and the value fit the field's type, so a rule means one thing in every evaluator."""
        spec = FIELDS[self.field]
        if self.op not in OPS_BY_KIND[spec.kind]:
            raise ValueError(f"{self.field} is a {spec.kind}: use one of {', '.join(OPS_BY_KIND[spec.kind])}")
        if spec.kind == "number":
            if isinstance(self.value, list):
                raise ValueError(f"{self.field} needs one number")
            try:
                self.value = float(self.value)
            except ValueError as exc:
                raise ValueError(f"{self.field} needs a number, not {self.value!r}") from exc
            if abs(self.value) > MAX_NUMBER:
                raise ValueError(f"{self.field}: {self.value:g} is out of range")
        elif spec.kind == "list":
            if isinstance(self.value, list) or not str(self.value).strip():
                raise ValueError(f"{self.field} includes one value")
            self.value = str(self.value)
        else:
            members = self.value if isinstance(self.value, list) else [str(self.value)]
            if self.op in ("is", "is_not") and len(members) != 1:
                raise ValueError(f"'{self.op}' takes one value; use in / not_in for several")
            unknown = [m for m in members if m not in spec.values]
            if unknown:
                raise ValueError(f"{self.field} is never {', '.join(unknown)}; values: {', '.join(spec.values)}")
            self.value = members if self.op in ("in", "not_in") else members[0]
        return self

    def holds(self, situation: dict[str, Any]) -> bool:
        if self.field not in situation or situation[self.field] is None:
            return False
        have = situation[self.field]
        want = self.value
        kind = FIELDS[self.field].kind
        if kind == "list":
            return str(want) in [str(x) for x in (have or [])]
        if kind == "enum":
            members = [str(x) for x in (want if isinstance(want, list) else [want])]
            return (str(have) in members) == (self.op in ("is", "in"))
        try:
            a, b = float(have), float(want)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return {"is": a == b, "is_not": a != b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[self.op]

    def words(self, lang: str = "en") -> str:
        spec = FIELDS[self.field]
        value = ", ".join(self.value) if isinstance(self.value, list) else str(self.value).replace("_", " ")
        if isinstance(self.value, float) and self.value.is_integer():
            value = str(int(self.value))
        if lang == "en":
            return f"{spec.en} {OPS_EN[self.op]} {value}"
        return f"{spec.hi} {value} {OPS_HI[self.op]}"


class Rule(BaseModel):
    """One owner rule, as the evaluators see it."""

    id: UUID
    capability: str
    effect: RuleEffect
    conditions: list[Condition] = Field(default_factory=list)
    says: str
    scope: Literal["global", "customer"] = "global"
    customer_id: UUID | None = None
    source: Literal["owner_instruction", "owner_decision", "autonomy_grant"] = "owner_instruction"

    def applies(self, situation: dict[str, Any]) -> bool:
        return all(c.holds(situation) for c in self.conditions)
