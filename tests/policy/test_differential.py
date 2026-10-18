"""Cedar against the reference evaluator (BETA_04 §7.5; CHK-POL-1), and the layering rules (CHK-POL-2)."""

from __future__ import annotations

import random

import pytest

import reflexo.policy.api as policy
from reflexo.policy.evaluate import evaluate as evaluate_cedar
from tests.policy.gen import rand_case, sets_for

CASES = 10_000


def _same(req: policy.PolicyRequest, rules: list[policy.Rule]) -> tuple[policy.Decision, policy.Decision]:
    return evaluate_cedar(req, sets_for(rules)), policy.reference.evaluate(req, rules)


@pytest.mark.check("CHK-POL-1")
def test_cedar_and_the_reference_evaluator_decide_alike_on_10000_requests() -> None:
    rng = random.Random(20261018)
    effects: dict[str, int] = {}
    mismatches = []
    for i in range(CASES):
        req, rules = rand_case(rng)
        cedar, ref = _same(req, rules)
        effects[cedar.effect] = effects.get(cedar.effect, 0) + 1
        if cedar != ref:
            mismatches.append((i, req.action, req.context, [r.model_dump(mode="json") for r in rules], cedar, ref))
    assert not mismatches, mismatches[:3]
    assert all(effects.get(e, 0) > 100 for e in ("act", "decline", "propose", "not_possible", "shadow")), effects


@pytest.mark.check("CHK-POL-1")
def test_every_generated_rule_compiles_to_valid_cedar() -> None:
    rng = random.Random(7)
    for _ in range(300):
        _, rules = rand_case(rng)
        for layer in ("customer", "shop"):
            text = policy.compile_rules(rules, layer=layer).cedar_text  # type: ignore[arg-type]
            assert policy.validate(text) == [], text
