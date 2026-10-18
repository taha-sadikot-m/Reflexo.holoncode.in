"""Playbook rules (ADR-015, POC_10 §4; BETA_04 §7): how *this* business wants the agent to act, taught by the owner.

A rule says, for one capability, whether the agent may do it (`allow`), must not (`deny`) or must ask first
(`propose`), optionally only when typed conditions hold. The rule language (`FIELDS`, `Condition`) lives in
`reflexo.policy`, which compiles active rules to Cedar for `decide()`. Rules are created, edited and undone
through knowledge change sets (entity type `playbook_rule`); every change recompiles the policy sets in the same
transaction. Before a new rule is read back to the owner, the rule checker verifies it (`check_change`).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text

import reflexo.policy.api as policy
from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.knowledge.changesets import apply_now
from reflexo.knowledge.policysets import active_rules

log = get_logger(__name__)
Effect = policy.RuleEffect
Op = policy.Op
SAFETY = policy.SAFETY
FieldSpec = policy.FieldSpec
FIELDS = policy.FIELDS
OPS_EN = policy.OPS_EN
OPS_HI = policy.OPS_HI
Condition = policy.Condition


class PlaybookRule(policy.Rule):
    source_ref: UUID | None = None

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
    wants the whole playbook)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, capability, effect, conditions, says, scope, customer_id, source, source_ref
                        FROM playbook_rules WHERE status = 'active'
                          AND (CAST(:cap AS text) IS NULL OR capability = :cap)
                          AND (scope = 'global' OR CAST(:c AS uuid) IS NULL OR customer_id = CAST(:c AS uuid))
                        ORDER BY created_at, id"""),
                {"cap": capability, "c": customer_id},
            )
        ).all()
    return [_row(r) for r in rows]


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


# ---------------------------------------------------------------- the rule checker before the read-back


class RuleExample(BaseModel):
    """One concrete situation where the change makes a difference, in playbook fields."""

    kind: Literal["newly_acts", "no_longer_acts", "newly_declines", "no_longer_declines"]
    capability: str
    facts: dict[str, Any]


class RuleCheck(BaseModel):
    status: Literal["ok", "blocked", "unchecked"]
    problem: Literal["impossible", "may_error", "invalid"] | None = None
    shadowed_by: str | None = None  # the rule id that makes the new one never decide
    examples: list[RuleExample] = Field(default_factory=list)
    analysis: dict[str, Any] = Field(default_factory=dict)  # stored on the policy set (`policy_sets.analysis`)


EXAMPLE_KINDS = {
    ("acts", "more"): "newly_acts",
    ("acts", "less"): "no_longer_acts",
    ("declines", "more"): "newly_declines",
    ("declines", "less"): "no_longer_declines",
}


async def check_change(business_id: UUID, proposed: policy.Rule, *, replaces: list[UUID] | None = None) -> RuleCheck:
    """Verify a rule before it is read back (BETA_04 §7.6): it can match, it can never error, and what it newly
    allows or forbids, with one concrete example each. `unchecked` = the checker could not run."""
    async with get_db().tenant_session(business_id) as s:
        old = await active_rules(s)
    gone = set(replaces or [])
    new = [r for r in old if r.id not in gone] + [proposed]
    layer: policy.RuleLayer = "customer" if proposed.scope == "customer" else "shop"
    compiled = policy.compile_rules(new, layer=layer).cedar_text
    pid = f"rule:{proposed.id}"

    lint = await policy.check("lint", set=compiled)
    errors = await policy.check("never-errors", set=compiled) if lint.available else lint
    pairs = [{"name": kind, "old": policy.view(old, kind, layer=layer), "new": policy.view(new, kind, layer=layer)}
             for kind in ("acts", "declines")]  # fmt: skip
    compare = await policy.check("compare", pairs=pairs) if errors.available else errors
    analysis = {r.command: r.model_dump(exclude={"comparisons"}) for r in (lint, errors, compare)}
    if not (lint.available and errors.available and compare.available):
        log.warning("rule_check_unavailable", business_id=str(business_id), error=compare.error or lint.error)
        return RuleCheck(status="unchecked", analysis=analysis)
    if lint.error or errors.error or compare.error:
        return RuleCheck(status="blocked", problem="invalid", analysis=analysis)
    mine = [f for f in lint.findings if f.policy_id == pid]
    if any(f.kind == "impossible" for f in mine):
        return RuleCheck(status="blocked", problem="impossible", analysis=analysis)
    if pid in errors.may_error:
        return RuleCheck(status="blocked", problem="may_error", analysis=analysis)
    examples = []
    for comparison in compare.comparisons:
        for direction in ("more", "less"):
            cexs = comparison.more if direction == "more" else comparison.less
            if cexs:
                facts = policy.example_facts(cexs[0], old, new, comparison.name, layer, direction)  # type: ignore[arg-type]
                examples.append(
                    RuleExample(
                        kind=EXAMPLE_KINDS[(comparison.name, direction)],
                        capability=policy.as_request(cexs[0]).action,
                        facts=facts,
                    )
                )
    shadow = next((f.by for f in mine if f.kind == "shadowed"), None)
    return RuleCheck(status="ok", shadowed_by=shadow, examples=examples, analysis=analysis)
