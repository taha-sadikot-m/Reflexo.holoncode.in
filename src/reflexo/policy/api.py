"""reflexo.policy: Cedar policies for `decide()` (BETA_04 §7; ADR-028). Pure: no database; callers pass rules
and entities.

- `compile_rules`: owner rules → one Cedar set per layer (§7.4).
- `evaluate`: layered evaluation on Cedar, or on the reference evaluator when `REFLEXO_POLICY_ENGINE=reference`.
- `check`: the rule checker (`tools/policy-check`) before a rule is read back (§7.6).
- `request`: builds a `PolicyRequest` from plain facts (the principal, the business, the resource, its customer).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from reflexo.common.config import get_settings
from reflexo.policy import reference
from reflexo.policy.check import (
    CheckReport,
    Comparison,
    Counterexample,
    LintFinding,
    View,
    as_request,
    check,
    checker_schema,
    example_facts,
    view,
)
from reflexo.policy.compile import cedar_str, compile_rules, source_hash
from reflexo.policy.evaluate import EMPTY, LayerSets, LoadedSet, file_set, load
from reflexo.policy.evaluate import evaluate as evaluate_cedar
from reflexo.policy.models import NAMESPACE, CompiledSet, Decision, Effect, EntityRef, Layer, PolicyRequest, RuleLayer
from reflexo.policy.rules import FIELDS, OPS_BY_KIND, OPS_EN, OPS_HI, SAFETY, Condition, FieldSpec, Op, Rule, RuleEffect
from reflexo.policy.schema import CEDAR_LOADED, ActionSpec, PolicyConfigError, actions, check_files, validate

__all__ = [
    "EMPTY",
    "FIELDS",
    "NAMESPACE",
    "OPS_BY_KIND",
    "OPS_EN",
    "OPS_HI",
    "SAFETY",
    "ActionSpec",
    "CheckReport",
    "Comparison",
    "CompiledSet",
    "Condition",
    "Counterexample",
    "Decision",
    "Effect",
    "EntityRef",
    "FieldSpec",
    "Layer",
    "LayerSets",
    "LintFinding",
    "LoadedSet",
    "Op",
    "PolicyConfigError",
    "PolicyRequest",
    "Rule",
    "RuleEffect",
    "RuleLayer",
    "View",
    "actions",
    "agent_entity",
    "as_request",
    "business_entity",
    "cedar_str",
    "check",
    "check_files",
    "checker_schema",
    "compile_rules",
    "customer_entity",
    "evaluate",
    "example_facts",
    "file_set",
    "link",
    "load",
    "member_entity",
    "order_entity",
    "reference",
    "source_hash",
    "start",
    "validate",
    "view",
]


def _entity(ref: EntityRef, attrs: dict[str, Any], business_id: UUID | None) -> dict[str, Any]:
    parents = [EntityRef(type="Business", id=str(business_id)).uid] if business_id else []
    return {"uid": ref.uid, "attrs": attrs, "parents": parents}


def link(ref: EntityRef) -> dict[str, Any]:
    return {"__entity": ref.uid}


def business_entity(business_id: UUID) -> tuple[EntityRef, dict[str, Any]]:
    ref = EntityRef(type="Business", id=str(business_id))
    return ref, {"uid": ref.uid, "attrs": {}, "parents": []}


def agent_entity(business_id: UUID, desk: str) -> tuple[EntityRef, dict[str, Any]]:
    ref = EntityRef(type="Agent", id=f"{business_id}:{desk}")
    biz = EntityRef(type="Business", id=str(business_id))
    return ref, _entity(ref, {"business": link(biz), "desk": desk}, business_id)


def member_entity(
    business_id: UUID, member_id: UUID, role: str, scopes: Sequence[str] = ()
) -> tuple[EntityRef, dict[str, Any]]:
    ref = EntityRef(type="Member", id=str(member_id))
    biz = EntityRef(type="Business", id=str(business_id))
    return ref, _entity(ref, {"business": link(biz), "role": role, "scopes": list(scopes)}, business_id)


def customer_entity(business_id: UUID, customer_id: UUID, orders_count: int) -> tuple[EntityRef, dict[str, Any]]:
    ref = EntityRef(type="Customer", id=str(customer_id))
    biz = EntityRef(type="Business", id=str(business_id))
    return ref, _entity(ref, {"business": link(biz), "orders_count": int(orders_count)}, business_id)


def order_entity(
    business_id: UUID,
    order_id: UUID,
    customer: EntityRef,
    *,
    status: str,
    payment_method: str | None,
    payment_status: str | None,
    total_rupees: int,
    categories: Sequence[str],
) -> tuple[EntityRef, dict[str, Any]]:
    ref = EntityRef(type="Order", id=str(order_id))
    biz = EntityRef(type="Business", id=str(business_id))
    attrs = {
        "business": link(biz),
        "customer": link(customer),
        "status": status,
        "payment_method": payment_method or "",  # "" = not known yet (compile.py)
        "payment_status": payment_status or "",
        "total_rupees": int(total_rupees),
        "categories": sorted(set(categories)),
    }
    return ref, _entity(ref, attrs, business_id)


def evaluate(req: PolicyRequest, sets: LayerSets, rules: Sequence[Rule] = ()) -> Decision:
    """Cedar by default; the reference evaluator only when chosen explicitly (it then needs the rules)."""
    if get_settings().policy_engine == "reference":
        return reference.evaluate(req, rules)
    return evaluate_cedar(req, sets)


def start() -> None:
    """At process start: Cedar must load and the repository layers must validate, unless the reference evaluator
    is chosen explicitly. There is no silent fallback."""
    if get_settings().policy_engine == "reference":
        return
    if not CEDAR_LOADED:
        raise PolicyConfigError("cedarpy cannot load; set REFLEXO_POLICY_ENGINE=reference to run without Cedar")
    check_files()
    for layer in ("platform", "default", "ceiling"):
        file_set(layer)
