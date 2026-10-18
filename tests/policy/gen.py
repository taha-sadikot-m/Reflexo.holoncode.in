"""Generated rules and requests for the differential and property tests (CHK-POL-1).

Requests cover every action of the schema with a valid principal and resource type, orders in every status,
missing and present optional facts, other businesses' resources, shadow mode and owner approvals. Rules are
random sentences of the closed playbook language, including fractional thresholds and customer scopes.
"""

from __future__ import annotations

import random
from typing import Any
from uuid import UUID

import reflexo.policy.api as policy

BUSINESS = UUID(int=1)
OTHER_BUSINESS = UUID(int=2)
CUSTOMERS = [UUID(int=100 + i) for i in range(3)]
CATEGORIES = ["cakes", "cookies", "kurtis", "gifts"]
STATUSES = [*policy.FIELDS["order.status"].values, "cancelled"]
NUMBER_PICKS = [-5, 0, 1, 2, 3, 23.5, 24, 48, 100, 499.5, 500, 650, 2000, 5000]


def rand_condition(rng: random.Random, field: str | None = None) -> policy.Condition:
    field = field or rng.choice(list(policy.FIELDS))
    spec = policy.FIELDS[field]
    op = rng.choice(policy.OPS_BY_KIND[spec.kind])
    value: Any
    if spec.kind == "number":
        value = float(rng.choice(NUMBER_PICKS))
    elif spec.kind == "list":
        value = rng.choice(CATEGORIES)
    elif op in ("in", "not_in"):
        value = rng.sample(list(spec.values), rng.randint(1, min(3, len(spec.values))))
    else:
        value = rng.choice(spec.values)
    return policy.Condition(field=field, op=op, value=value)


def rand_rule(rng: random.Random, capability: str, n: int) -> policy.Rule:
    customer = rng.choice(CUSTOMERS) if rng.random() < 0.3 else None
    return policy.Rule(
        id=UUID(int=10_000 + n),
        capability=capability,
        effect=rng.choice(["allow", "deny", "propose"]),
        conditions=[rand_condition(rng) for _ in range(rng.choice([0, 1, 1, 2, 3]))],
        says=rng.choice(["cancel kar do", 'say "no" to "late" ones', "पूछ लेना", "ok\nfine", "₹500 tak theek"]),
        scope="customer" if customer else "global",
        customer_id=customer,
        source=rng.choice(["owner_instruction", "owner_decision"]),
    )


def rand_request(rng: random.Random) -> policy.PolicyRequest:
    action = rng.choice(sorted(policy.actions()))
    spec = policy.actions()[action]
    business = OTHER_BUSINESS if rng.random() < 0.05 else BUSINESS  # the resource's business
    entities: list[dict[str, Any]] = []
    if "Member" in spec.principals and ("Agent" not in spec.principals or rng.random() < 0.3):
        principal, ent = policy.member_entity(BUSINESS, UUID(int=500), rng.choice(["owner", "staff"]))
    else:
        principal, ent = policy.agent_entity(BUSINESS, rng.choice(["customer", "owner", "voice"]))
    entities.append(ent)
    for b in {BUSINESS, business}:
        entities.append(policy.business_entity(b)[1])
    customer_id = rng.choice(CUSTOMERS)
    customer, cent = policy.customer_entity(business, customer_id, rng.randint(0, 5))
    resource_type = spec.resources[0]
    if resource_type == "Order":
        resource, ent = policy.order_entity(
            business,
            UUID(int=900),
            customer,
            status=rng.choice(STATUSES),
            payment_method=rng.choice([None, *policy.FIELDS["order.payment_method"].values]),
            payment_status=rng.choice([None, *policy.FIELDS["order.payment_status"].values]),
            total_rupees=rng.choice([0, 1, 499, 500, 650, 1999, 2000, 5000, 9000]),
            categories=rng.sample(CATEGORIES, rng.randint(0, 2)),
        )
        entities += [cent, ent]
    elif resource_type == "Customer":
        resource = customer
        entities.append(cent)
    elif resource_type == "Business":
        resource = policy.EntityRef(type="Business", id=str(business))
    else:  # Product, Shipment, Booking, Comment
        resource = policy.EntityRef(type=resource_type, id=str(UUID(int=950)))
        biz = policy.link(policy.EntityRef(type="Business", id=str(business)))
        attrs: dict[str, Any] = {"business": biz}
        if resource_type == "Product":
            attrs["categories"] = rng.sample(CATEGORIES, 1)
        elif resource_type == "Comment":
            attrs |= {"origin": "customer", "channel": "instagram"}
        elif resource_type == "Booking":
            attrs["customer"] = policy.link(customer)
            entities.append(cent)
        elif resource_type == "Shipment":
            order, oent = policy.order_entity(
                business, UUID(int=901), customer, status=rng.choice(STATUSES), payment_method="cod",
                payment_status="cod_pending", total_rupees=650, categories=["cakes"],
            )  # fmt: skip
            attrs |= {"order": policy.link(order), "status": "in_transit"}
            entities += [cent, oent]
        parents = [{"type": "Reflexo::Business", "id": str(business)}]
        entities.append({"uid": resource.uid, "attrs": attrs, "parents": parents})
    context: dict[str, Any] = {
        "requested_by": rng.choice(["customer_turn", "owner_turn", "team_turn", "automation", "voice"]),
        "owner_approved": rng.random() < 0.2,
    }
    for name in ("hours_until_delivery", "hours_since_placed", "refunds_today_rupees", "cancellations_this_week"):
        if rng.random() < 0.6:
            context[name] = rng.choice([-30, 0, 1, 2, 3, 23, 24, 25, 47, 48, 72, 499, 500, 650, 5000, 5001])
    if rng.random() < 0.5:
        context["customer_mode"] = rng.choice(["tiered", "shadow", "draft_all"])
    if rng.random() < 0.3:
        context["unit_granted"] = rng.random() < 0.5
    return policy.PolicyRequest(
        principal=principal,
        action=action,
        resource=resource,
        context=context,
        entities=entities,
        owner_approved=bool(context["owner_approved"]),
    )


def rand_case(rng: random.Random) -> tuple[policy.PolicyRequest, list[policy.Rule]]:
    req = rand_request(rng)
    pool = [req.action] * 3 + rng.sample(sorted(policy.actions()), 2)
    rules = [rand_rule(rng, rng.choice(pool), i) for i in range(rng.randint(0, 6))]
    return req, rules


def sets_for(rules: list[policy.Rule]) -> policy.LayerSets:
    customer = policy.compile_rules(rules, layer="customer")
    shop = policy.compile_rules(rules, layer="shop")
    return policy.LayerSets(policy.load("customer", customer.cedar_text), policy.load("shop", shop.cedar_text))
