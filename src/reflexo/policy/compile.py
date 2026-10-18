"""Owner rules to Cedar, one rule to one policy (BETA_04 §7.4).

The rule language is closed (`rules.FIELDS`), so this is a direct translation. Two encodings keep "a rule never
fires on data it cannot see" exact:
- an optional context field is always guarded (`context has f && ...`);
- an order attribute the order does not have yet (a draft's payment method) is the empty string, and every
  comparison that could hold for "" is guarded with `!= ""`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence

from reflexo.policy.models import NAMESPACE, CompiledSet, RuleLayer
from reflexo.policy.rules import FIELDS, Condition, Rule
from reflexo.policy.schema import PolicyConfigError, actions

# where an order and a customer are, seen from each resource type
ORDER_PATH = {"Order": "resource", "Shipment": "resource.order"}
CUSTOMER_PATH = {
    "Order": "resource.customer",
    "Customer": "resource",
    "Booking": "resource.customer",
    "Shipment": "resource.order.customer",
}
CONNECTOR_RESOURCE = "ConnectorTool"


def cedar_str(value: str) -> str:
    """A Cedar string literal."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def resource_type(capability: str, connector_actions: Iterable[str] = ()) -> str:
    if capability in set(connector_actions):
        return CONNECTOR_RESOURCE
    spec = actions().get(capability)
    if spec is None:
        raise PolicyConfigError(f"no Cedar action for capability {capability}")
    if len(spec.resources) != 1:
        raise PolicyConfigError(f"{capability} applies to several resource types; rules need one")
    return spec.resources[0]


def _whole(op: str, value: float) -> tuple[str, int] | bool:
    """The whole-number comparison equivalent to `x <op> value` for every whole x (or a constant)."""
    if value.is_integer():
        return {"is": "==", "is_not": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[op], int(value)
    if op == "is":
        return False
    if op == "is_not":
        return True
    return {
        "lt": ("<", math.ceil(value)),
        "lte": ("<=", math.floor(value)),
        "gt": (">", math.floor(value)),
        "gte": (">=", math.ceil(value)),
    }[op]


def condition_expr(c: Condition, resource: str) -> str:
    spec = FIELDS[c.field]
    guard = ""
    if spec.on == "context":
        guard = f"context has {spec.attr} && "
        path = f"context.{spec.attr}"
    elif spec.on == "order":
        if resource not in ORDER_PATH:
            return "false"
        path = f"{ORDER_PATH[resource]}.{spec.attr}"
    else:
        if resource not in CUSTOMER_PATH:
            return "false"
        path = f"{CUSTOMER_PATH[resource]}.{spec.attr}"

    if spec.kind == "list":
        expr = f"{path}.contains({cedar_str(str(c.value))})"
    elif spec.kind == "enum":
        members = c.value if isinstance(c.value, list) else [str(c.value)]
        listed = "[" + ", ".join(cedar_str(m) for m in members) + "]"
        expr = {
            "is": f"{path} == {cedar_str(members[0])}",
            "is_not": f'{path} != "" && {path} != {cedar_str(members[0])}',
            "in": f"{listed}.contains({path})",
            "not_in": f'{path} != "" && !{listed}.contains({path})',
        }[c.op]
    else:
        whole = _whole(c.op, float(c.value))  # type: ignore[arg-type]
        if isinstance(whole, bool):
            if not whole:
                return "false"
            return f"context has {spec.attr}" if spec.on == "context" else "true"
        sym, n = whole
        expr = f"{path} {sym} {n}"
    return f"({guard}{expr})"


def scope_expr(rule: Rule, resource: str) -> str | None:
    if rule.scope != "customer" or rule.customer_id is None:
        return None
    if resource not in CUSTOMER_PATH:
        return "false"
    return f'{CUSTOMER_PATH[resource]} == {NAMESPACE}::Customer::"{rule.customer_id}"'


def rule_policy(rule: Rule, connector_actions: Iterable[str] = ()) -> str:
    resource = resource_type(rule.capability, connector_actions)
    parts = [p for p in [scope_expr(rule, resource)] if p] + [condition_expr(c, resource) for c in rule.conditions]
    head = "forbid" if rule.effect == "deny" else "permit"
    lines = [
        f'@id("rule:{rule.id}")',
        f"@says({cedar_str(rule.says)})",
        f'@source("{rule.source}")',
        f'@effect("{rule.effect}")',
        f"{head} (principal, action == {NAMESPACE}::Action::{cedar_str(rule.capability)}, resource)",
    ]
    if parts:
        lines.append("  when { " + " && ".join(parts) + " };")
    else:
        lines[-1] += ";"
    return "\n".join(lines)


def source_hash(rules: Sequence[Rule]) -> bytes:
    canon = [r.model_dump(mode="json") for r in rules]
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).digest()


def compile_rules(rules: Sequence[Rule], *, layer: RuleLayer, connector_actions: Iterable[str] = ()) -> CompiledSet:
    """The rules of one layer (customer-scoped or shop-wide), in the order given (oldest first)."""
    connector = list(connector_actions)
    want = "customer" if layer == "customer" else "global"
    mine = [r for r in rules if r.scope == want]
    text = "\n\n".join(rule_policy(r, connector) for r in mine)
    return CompiledSet(
        layer=layer,
        cedar_text=text + ("\n" if text else ""),
        source_hash=source_hash(mine),
        rule_ids=[f"rule:{r.id}" for r in mine],
    )
