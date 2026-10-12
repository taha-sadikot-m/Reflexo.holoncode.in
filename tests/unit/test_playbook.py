"""Playbook conditions (ADR-015): typed, deterministic, and read back to the owner exactly as they behave."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from reflexo.knowledge.playbook import Condition, PlaybookRule

SITUATION = {
    "order.status": "confirmed",
    "order.payment_method": "cod",
    "order.total_rupees": 600.0,
    "order.hours_until_delivery": 30.5,
    "order.categories": ["cakes"],
    "customer.orders_count": 0,
}


@pytest.mark.parametrize(
    ("field", "op", "value", "holds"),
    [
        ("order.status", "is", "confirmed", True),
        ("order.status", "is_not", "confirmed", False),
        ("order.status", "in", ["draft", "confirmed"], True),
        ("order.status", "not_in", ["preparing", "out_for_delivery"], True),
        ("order.hours_until_delivery", "gte", 24, True),
        ("order.hours_until_delivery", "lt", 24, False),
        ("order.total_rupees", "lte", 600, True),
        ("order.categories", "includes", "cakes", True),
        ("order.categories", "includes", "cookies", False),
        ("customer.orders_count", "gt", 0, False),
        ("order.hours_since_placed", "lt", 1, False),  # not in the situation: a rule never fires on unseen data
    ],
)
def test_condition_holds(field: str, op: str, value: object, holds: bool) -> None:
    assert Condition.model_validate({"field": field, "op": op, "value": value}).holds(SITUATION) is holds


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Condition(field="order.vibes", op="is", value="good")


def test_readback_says_exactly_what_the_rule_does() -> None:
    rule = PlaybookRule(
        id=uuid4(),
        capability="order.cancel",
        effect="allow",
        conditions=[
            Condition(field="order.status", op="not_in", value=["preparing", "out_for_delivery"]),
            Condition(field="order.hours_until_delivery", op="gte", value=24.0),
        ],
        says="cancel kar do agar bana nahi hai",
        source="owner_decision",
    )
    assert rule.applies(SITUATION)
    assert rule.readback("Cancelling an order") == (
        "Cancelling an order: I do it myself when the order's status is not one of preparing, out_for_delivery "
        "and hours left until delivery is at least 24"
    )
    assert rule.readback("Order cancel karna", "hi-Latn").startswith("Order cancel karna: jab order ka status")
    always = rule.model_copy(update={"conditions": [], "effect": "propose"})
    assert always.readback("Cancelling an order") == "Cancelling an order: I ask you first — always"
