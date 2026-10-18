"""The reference evaluator (BETA_04 §7.5): the POC's Python decision logic, kept to test Cedar against.

It reproduces, from the same `PolicyRequest`, what the POC decided: capability invariants (now the platform
layer), `playbook.match` (this customer's rules before the shop's; the safest effect wins a tie), the risk
defaults, and the beta's ceilings. It never touches Cedar: the differential test (CHK-POL-1) checks both agree
on generated requests. `REFLEXO_POLICY_ENGINE=reference` runs decisions on it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from reflexo.policy.models import NAMESPACE, Decision, Effect, Layer, PolicyRequest
from reflexo.policy.rules import FIELDS, Rule
from reflexo.policy.schema import actions, layer_text

CANCEL_CLOSED = ("out_for_delivery", "delivered")
CHANGE_CLOSED = ("cancelled", "out_for_delivery", "delivered")
DRAFTABLE = ("order.cancel", "order.change")


def _ref(value: Any) -> dict[str, str] | None:
    return value.get("__entity") if isinstance(value, dict) else None


def _attrs(req: PolicyRequest, uid: dict[str, str] | None) -> dict[str, Any] | None:
    e = req.entity(uid) if uid else None
    return dict(e.get("attrs") or {}) if e else None


def _type(req: PolicyRequest) -> str:
    return req.resource.type


def order_of(req: PolicyRequest) -> dict[str, Any] | None:
    resource = _attrs(req, req.resource.uid)
    if _type(req) == "Order":
        return resource
    if _type(req) == "Shipment" and resource:
        return _attrs(req, _ref(resource.get("order")))
    return None


def customer_uid(req: PolicyRequest) -> dict[str, str] | None:
    t = _type(req)
    if t == "Customer":
        return req.resource.uid
    if t in ("Order", "Booking"):
        return _ref((_attrs(req, req.resource.uid) or {}).get("customer"))
    if t == "Shipment":
        return _ref((order_of(req) or {}).get("customer"))
    return None


def situation(req: PolicyRequest) -> dict[str, Any]:
    """The playbook's situation fields, read from the request exactly where the compiled Cedar reads them."""
    order = order_of(req)
    customer = _attrs(req, customer_uid(req))
    out: dict[str, Any] = {}
    for name, spec in FIELDS.items():
        source = {"order": order, "customer": customer, "context": req.context}[spec.on]
        if source is None or spec.attr not in source:
            continue
        value = source[spec.attr]
        out[name] = None if value == "" else value
    return out


def _rule_customer(rule: Rule) -> dict[str, str] | None:
    return {"type": f"{NAMESPACE}::Customer", "id": str(rule.customer_id)} if rule.customer_id else None


@lru_cache(maxsize=1)
def _reasons() -> dict[str, str]:
    """The invariants' wording, read from the platform file's annotations (not evaluated by Cedar here)."""
    text = layer_text("platform")
    return dict(re.findall(r'@id\("([^"]+)"\)\s*@reason\("([^"]*)"\)', text))


def _platform(req: PolicyRequest) -> list[tuple[str, str]]:
    """Matching invariants in file order, as (id, reason)."""
    order = order_of(req) if _type(req) == "Order" else None
    status = (order or {}).get("status")
    hits = []
    if req.action == "order.cancel" and order is not None and status in CANCEL_CLOSED:
        hits.append("inv.cancel_closed")
    if req.action == "order.change" and order is not None and status in CHANGE_CLOSED:
        hits.append("inv.change_closed")
    if req.action == "refund.request" and order is not None and order.get("payment_status") != "paid":
        hits.append("inv.refund_unpaid")
    principal_business = _ref((_attrs(req, req.principal.uid) or {}).get("business"))
    resource = req.entity(req.resource.uid)
    inside = req.resource.uid == principal_business or (
        resource is not None and principal_business in (resource.get("parents") or [])
    )
    if not inside:
        hits.append("iso.business")
    return [(pid, _reasons()[pid]) for pid in hits]


def _rules_layer(
    req: PolicyRequest, rules: Sequence[Rule], scope: str, sit: dict[str, Any]
) -> tuple[Effect, list[str], str | None] | None:
    customer = customer_uid(req)
    matching = [
        r
        for r in rules
        if r.capability == req.action
        and r.scope == scope
        and (scope == "global" or (customer is not None and _rule_customer(r) == customer))
        and r.applies(sit)
    ]
    if not matching:
        return None
    for effect, rule_effect in (("decline", "deny"), ("propose", "propose"), ("act", "allow")):
        chosen = [r for r in matching if r.effect == rule_effect]
        if chosen:
            return effect, [f"rule:{r.id}" for r in chosen], chosen[0].says  # type: ignore[return-value]
    return None


def _default(req: PolicyRequest, risk: str | None) -> tuple[Effect, list[str]] | None:
    draft = req.action in DRAFTABLE and _type(req) == "Order" and (order_of(req) or {}).get("status") == "draft"
    if draft:
        return "act", ["def.draft"]
    if risk == "routine":
        return "act", ["def.routine"]
    if risk == "commitment":
        return "propose", ["def.commitment"]
    if risk == "money":
        return "propose", ["def.money"]
    return None


def _ceilings(req: PolicyRequest, risk: str | None) -> list[tuple[str, str]]:
    ctx = req.context
    by = ctx.get("requested_by")
    found = []
    if ctx.get("customer_mode") == "shadow" and by in ("customer_turn", "voice") and not ctx.get("unit_granted"):
        found.append(("ceil.shadow", "shadow"))
    if risk == "commitment" and _type(req) == "ConnectorTool" and by == "customer_turn":
        found.append(("ceil.customer_writes", "cap"))
    if risk == "money" and by == "customer_turn":
        found.append(("ceil.money", "cap"))
    return found


def evaluate(req: PolicyRequest, rules: Sequence[Rule], connector_risks: dict[str, str] | None = None) -> Decision:
    spec = actions().get(req.action)
    risk = spec.risk if spec else (connector_risks or {}).get(req.action)
    invariants = _platform(req)
    if invariants:
        return Decision(
            effect="not_possible", layer="platform", policy_ids=[i for i, _ in invariants], reason=invariants[0][1]
        )
    sit = situation(req)
    effect: Effect = "propose"
    layer: Layer = "default"
    ids: list[str] = []
    says: str | None = None
    for scope, name in (("customer", "customer"), ("global", "shop")):
        found = _rules_layer(req, rules, scope, sit)
        if found:
            effect, ids, says = found
            layer = name  # type: ignore[assignment]
            break
    else:
        default = _default(req, risk)
        if default:
            effect, ids = default
    ceilings = _ceilings(req, risk)
    shadowing = [i for i, kind in ceilings if kind == "shadow"]
    if effect in ("act", "propose") and shadowing:
        effect, layer, ids = "shadow", "ceiling", shadowing
    elif effect == "act" and ceilings:
        effect, layer, ids = "propose", "ceiling", [i for i, _ in ceilings]
    if effect == "propose" and req.owner_approved:
        effect = "act"
    return Decision(effect=effect, layer=layer, policy_ids=ids, says=says)
