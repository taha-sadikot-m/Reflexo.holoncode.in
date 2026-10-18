"""The rule compiler, property-tested (BETA_08 §3.4; CHK-POL-1): for any rule of the closed playbook language and
any situation, Cedar's effect equals the reference evaluator's. Hypothesis shrinks a mismatch to its smallest
rule and situation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import reflexo.policy.api as policy
from reflexo.policy.evaluate import evaluate
from tests.policy.gen import BUSINESS, CATEGORIES, CUSTOMERS, STATUSES, sets_for

NUMBERS = st.one_of(st.integers(-100, 10_000), st.floats(-100, 10_000, allow_nan=False).map(lambda v: round(v, 1)))


@st.composite
def conditions(draw: Any) -> policy.Condition:
    field = draw(st.sampled_from(sorted(policy.FIELDS)))
    spec = policy.FIELDS[field]
    op = draw(st.sampled_from(policy.OPS_BY_KIND[spec.kind]))
    if spec.kind == "number":
        value: Any = float(draw(NUMBERS))
    elif spec.kind == "list":
        value = draw(st.sampled_from(CATEGORIES))
    elif op in ("in", "not_in"):
        value = draw(st.lists(st.sampled_from(spec.values), min_size=1, max_size=3, unique=True))
    else:
        value = draw(st.sampled_from(spec.values))
    return policy.Condition(field=field, op=op, value=value)


@st.composite
def rules(draw: Any) -> list[policy.Rule]:
    out = []
    for i in range(draw(st.integers(1, 4))):
        customer = draw(st.one_of(st.none(), st.sampled_from(CUSTOMERS)))
        out.append(
            policy.Rule(
                id=UUID(int=i + 1),
                capability=draw(st.sampled_from(["order.cancel", "order.change", "refund.request"])),
                effect=draw(st.sampled_from(["allow", "deny", "propose"])),
                conditions=draw(st.lists(conditions(), max_size=3)),
                says=draw(st.text(max_size=20)),
                scope="customer" if customer else "global",
                customer_id=customer,
            )
        )
    return out


@st.composite
def requests(draw: Any) -> policy.PolicyRequest:
    agent, aent = policy.agent_entity(BUSINESS, "customer")
    cref, cent = policy.customer_entity(BUSINESS, draw(st.sampled_from(CUSTOMERS)), draw(st.integers(0, 20)))
    oref, oent = policy.order_entity(
        BUSINESS,
        UUID(int=900),
        cref,
        status=draw(st.sampled_from(STATUSES)),
        payment_method=draw(st.sampled_from([None, "cod", "razorpay_link"])),
        payment_status=draw(st.sampled_from([None, "unpaid", "pending", "cod_pending", "paid"])),
        total_rupees=draw(st.integers(0, 10_000)),
        categories=draw(st.lists(st.sampled_from(CATEGORIES), max_size=3, unique=True)),
    )
    context: dict[str, Any] = {
        "requested_by": draw(st.sampled_from(["customer_turn", "owner_turn", "automation", "voice"])),
        "owner_approved": draw(st.booleans()),
    }
    for name in ("hours_until_delivery", "hours_since_placed", "refunds_today_rupees", "cancellations_this_week"):
        value = draw(st.one_of(st.none(), st.integers(-100, 10_000)))
        if value is not None:
            context[name] = value
    return policy.PolicyRequest(
        principal=agent,
        action=draw(st.sampled_from(["order.cancel", "order.change", "refund.request"])),
        resource=oref,
        context=context,
        entities=[policy.business_entity(BUSINESS)[1], aent, cent, oent],
        owner_approved=bool(context["owner_approved"]),
    )


@pytest.mark.check("CHK-POL-1")
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rs=rules(), req=requests())
def test_cedar_and_the_reference_agree_on_any_rule_and_situation(
    rs: list[policy.Rule], req: policy.PolicyRequest
) -> None:
    assert evaluate(req, sets_for(rs)) == policy.reference.evaluate(req, rs)
