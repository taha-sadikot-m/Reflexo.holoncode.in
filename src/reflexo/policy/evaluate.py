"""Layered evaluation on Cedar (BETA_04 §7.3, §7.5).

Cedar has no priorities, but our rules need precedence: platform limits, then this customer's rules, then the
shop's rules, then the risk defaults, then the ceilings; within a layer the safest effect wins. Each layer stays
its own policy set (that is what is stored, compiled and analysed). For speed, `decide()` asks Cedar once over a
combined view of the layers: the platform's policies stay `forbid`, and every other policy enters as a `permit`
whose id names its layer and whose annotations keep its effect. A platform forbid then makes the answer Deny with
the forbids as reasons; otherwise every matching policy of every layer is in the reasons, and the precedence
below is applied to them. The differential test (CHK-POL-1) holds this to the reference evaluator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from typing import Any

from reflexo.policy.models import NAMESPACE, Decision, Effect, Layer, PolicyRequest
from reflexo.policy.schema import layer_text, schema_handle

try:
    import cedarpy
except ImportError:  # api.start() refuses to run on Cedar without it
    cedarpy = None  # type: ignore[assignment]

RULE_LAYERS: tuple[Layer, ...] = ("customer", "shop", "default")


@dataclass(frozen=True)
class LoadedSet:
    layer: str
    text: str
    handle: Any  # cedarpy.PolicySet, or None for an empty set
    annotations: dict[str, dict[str, str]] = field(default_factory=dict)  # 'policy3' → {'id': ..., 'effect': ...}
    policies: dict[str, Any] = field(default_factory=dict)  # 'policy3' → its Cedar JSON

    def ids(self, reasons: list[str]) -> list[str]:
        return [self.annotations.get(r, {}).get("id", r) for r in reasons]


def load(layer: str, text: str) -> LoadedSet:
    """Parse once; the handle is reused for every request (cached by the caller per set version)."""
    if not text.strip():
        return LoadedSet(layer, text, None)
    handle = cedarpy.PolicySet.from_str(text)
    static = json.loads(cedarpy.policies_to_json_str(text)).get("staticPolicies", {})
    annotations = {pid: dict(p.get("annotations") or {}) for pid, p in static.items()}
    return LoadedSet(layer, text, handle, annotations, static)


@lru_cache(maxsize=4)
def file_set(layer: str) -> LoadedSet:
    return load(layer, layer_text(layer))


def _order(pid: str) -> tuple[int, str]:
    digits = "".join(ch for ch in pid.rsplit(":", 1)[-1] if ch.isdigit())
    return (int(digits) if digits else 0, pid)


@dataclass(frozen=True)
class Combined:
    handle: Any
    meta: dict[str, tuple[str, dict[str, str]]]  # combined id → (layer, annotations)


@lru_cache(maxsize=512)
def _combine(customer: str, shop: str) -> Combined:
    sets = {
        "platform": file_set("platform"),
        "customer": load("customer", customer),
        "shop": load("shop", shop),
        "default": file_set("default"),
        "ceiling": file_set("ceiling"),
    }
    static: dict[str, Any] = {}
    meta: dict[str, tuple[str, dict[str, str]]] = {}
    for layer, s in sets.items():
        for pid, policy in s.policies.items():
            annotations = dict(policy.get("annotations") or {})
            if layer != "platform":
                if policy["effect"] == "forbid":
                    annotations["effect"] = "deny"
                policy = {**policy, "effect": "permit"}
            key = f"{layer}:{pid}"
            static[key] = policy
            meta[key] = (layer, annotations)
    body = {"staticPolicies": static, "templates": {}, "templateLinks": []}
    return Combined(cedarpy.PolicySet.from_json_str(json.dumps(body)), meta)


@dataclass(frozen=True)
class LayerSets:
    customer: LoadedSet
    shop: LoadedSet
    fragment: str = ""  # the business's generated connector actions

    @property
    def platform(self) -> LoadedSet:
        return file_set("platform")

    @property
    def default(self) -> LoadedSet:
        return file_set("default")

    @property
    def ceiling(self) -> LoadedSet:
        return file_set("ceiling")

    @cached_property
    def combined(self) -> Combined:
        return _combine(self.customer.text, self.shop.text)


EMPTY = LayerSets(LoadedSet("customer", "", None), LoadedSet("shop", "", None))


def evaluate(req: PolicyRequest, sets: LayerSets) -> Decision:
    schema = schema_handle(sets.fragment)
    combined = sets.combined
    entities = cedarpy.Entities.from_json_str(json.dumps(req.entities), schema)
    request = {
        "principal": req.principal.cedar,
        "action": f'{NAMESPACE}::Action::"{req.action}"',
        "resource": req.resource.cedar,
        "context": req.context,
    }
    answer = cedarpy.is_authorized(request, combined.handle, entities, schema)
    errored = bool(answer.diagnostics.errors)
    by_layer: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for r in sorted(answer.diagnostics.reasons, key=_order):
        source, annotations = combined.meta[r]
        by_layer.setdefault(source, []).append((annotations.get("id", r), annotations))

    if not answer.allowed and by_layer.get("platform"):
        first = by_layer["platform"][0][1]
        return Decision(
            effect="not_possible", layer="platform", policy_ids=[pid for pid, _ in by_layer["platform"]],
            reason=first.get("reason"),
        )  # fmt: skip

    effect: Effect = "propose"
    layer: Layer = "default"
    ids: list[str] = []
    says: str | None = None
    for name in RULE_LAYERS:
        matched = by_layer.get(name)
        if not matched:
            continue
        layer = name
        for candidate, wanted in (("decline", "deny"), ("propose", "propose"), ("act", None)):
            chosen = [(pid, a) for pid, a in matched if wanted is None or a.get("effect") == wanted]
            if chosen:
                effect, ids, says = candidate, [pid for pid, _ in chosen], chosen[0][1].get("says")  # type: ignore[assignment]
                break
        break

    ceiling = by_layer.get("ceiling", [])
    shadowing = [pid for pid, a in ceiling if a.get("effect") == "shadow"]
    if effect in ("act", "propose") and shadowing:
        effect, layer, ids = "shadow", "ceiling", shadowing
    elif effect == "act" and ceiling:
        effect, layer, ids = "propose", "ceiling", [pid for pid, _ in ceiling]

    if effect == "act" and errored:  # a policy that cannot be evaluated never lets an action through unasked
        effect = "propose"
    if effect == "propose" and req.owner_approved:
        effect = "act"
    return Decision(effect=effect, layer=layer, policy_ids=ids, says=says)
