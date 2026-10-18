"""The owner's rules and the rule checker (BETA_04 §7.6).

Before a rule is read back, `review` runs the checker. A rule that can never match, or that could fail to
evaluate, is not offered. Otherwise the read-back carries one concrete example of what the change newly allows
or forbids, in the playbook's own field words, never as Cedar. When the checker cannot run, the rule is saved as
"ask me first" whatever was asked, the owner is told, and `recheck` tries again every hour; once the check
passes, the owner gets the rule as they asked it to confirm.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field

import reflexo.approvals.api as approvals
import reflexo.knowledge.api as knowledge
from reflexo.agent import messages
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer
from reflexo.common.db import get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.logging import get_logger

log = get_logger(__name__)
RECHECK_QUEUE = "playbook.recheck"
RECHECK_EVERY_S = 3600
RECHECK_MAX_TRIES = 72  # three days of hourly tries, then the rule simply stays "ask me first"
Condition = knowledge.playbook.Condition


class Difference(BaseModel):
    kind: str  # newly_acts | no_longer_acts | newly_declines | no_longer_declines
    when: str  # the example, in the playbook's field words
    line: str  # the read-back line: "This also lets me do it myself when the order total (₹) is 500."


class Review(BaseModel):
    status: Literal["ok", "blocked", "unchecked"]
    problem: str | None = None
    effect: str  # what will be saved: the asked effect, or "propose" while unchecked
    differences: list[Difference] = Field(default_factory=list)
    shadowed_by: str | None = None  # the read-back of the rule that makes this one never decide
    analysis: dict[str, Any] = Field(default_factory=dict)


@lru_cache(maxsize=1)
def _vocabulary() -> dict[str, dict[str, str]]:
    path = Path(get_settings().policy_dir) / "readback.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def difference_line(kind: str, when: str, lang: str = "en") -> str:
    words = _vocabulary().get(lang) or _vocabulary()["en"]
    return f"{words[kind]} {when}."


def example_words(facts: dict[str, Any], lang: str = "en") -> str:
    """The facts of an example as the read-back writes conditions ("the order's status is confirmed and ...")."""
    parts: list[str] = []
    for name, value in facts.items():
        spec = knowledge.playbook.FIELDS.get(name)
        if spec is None or value is None:
            continue
        if spec.kind == "list":
            parts += [Condition(field=name, op="includes", value=str(v)).words(lang) for v in value or []]
        else:
            parts.append(Condition(field=name, op="is", value=value).words(lang))
    return (" and " if lang == "en" else " aur ").join(parts)


async def review(
    business_id: UUID, rule: knowledge.playbook.PlaybookRule, *, replaces: list[UUID] | None = None, lang: str = "en"
) -> Review:
    checked = await knowledge.playbook.check_change(business_id, rule, replaces=replaces)
    shadow_text = None
    if checked.shadowed_by:
        shadow_id = checked.shadowed_by.removeprefix("rule:")
        found = [r for r in await knowledge.playbook.rules(business_id) if str(r.id) == shadow_id]
        if found:
            from reflexo.agent.capabilities import CAPABILITIES

            cap = CAPABILITIES.get(found[0].capability)
            shadow_text = found[0].readback(cap.label if cap else found[0].capability, lang)
    return Review(
        status=checked.status,
        problem=checked.problem,
        effect=rule.effect if checked.status == "ok" else "propose",
        differences=[
            Difference(kind=e.kind, when=w, line=difference_line(e.kind, w, lang))
            for e in checked.examples
            if (w := example_words(e.facts, lang))
        ],
        shadowed_by=shadow_text,
        analysis=checked.analysis,
    )


async def schedule_recheck(
    business_id: UUID, rule_id: UUID, asked_effect: str, *, change_set_id: UUID | None, member_id: UUID | None,
    tries: int = 0,
) -> None:  # fmt: skip
    enqueuer = ports.registry.optional(JobEnqueuer)  # type: ignore[type-abstract]
    if enqueuer is None:
        return
    await enqueuer.enqueue(
        RECHECK_QUEUE,
        {
            "business_id": str(business_id),
            "rule_id": str(rule_id),
            "asked_effect": asked_effect,
            "change_set_id": str(change_set_id) if change_set_id else None,
            "member_id": str(member_id) if member_id else None,
            "tries": tries,
        },
        business_id=business_id,
        priority=PriorityClass.P3,
        dedupe_key=f"rulecheck:{rule_id}:{tries}",
        delay_s=RECHECK_EVERY_S,
    )


async def recheck(payload: dict[str, Any]) -> None:
    """Job `playbook.recheck`: the checker was unavailable when a rule was saved as "ask me first"."""
    bid = UUID(payload["business_id"])
    rule_id = UUID(payload["rule_id"])
    tries = int(payload.get("tries") or 0)
    member_id = UUID(payload["member_id"]) if payload.get("member_id") else None
    cs_id = UUID(payload["change_set_id"]) if payload.get("change_set_id") else None

    async def again() -> None:
        if tries + 1 < RECHECK_MAX_TRIES:
            await schedule_recheck(bid, rule_id, payload["asked_effect"], change_set_id=cs_id, member_id=member_id,
                                   tries=tries + 1)  # fmt: skip

    rule = next((r for r in await knowledge.playbook.rules(bid) if r.id == rule_id), None)
    if rule is None:
        if cs_id is not None and await knowledge.change_set_status(bid, cs_id) == "draft":
            await again()  # the owner has not confirmed the rule yet
        return
    if rule.effect != "propose":
        return  # changed since: nothing to restore
    asked = rule.model_copy(update={"effect": payload["asked_effect"]})
    result = await review(bid, asked, replaces=[rule.id])
    await knowledge.policysets.record_analysis(bid, result.analysis)
    if result.status == "unchecked":
        await again()
        return
    if result.status == "blocked":
        log.info("rule_recheck_blocked", business_id=str(bid), rule_id=str(rule_id), problem=result.problem)
        return
    from reflexo.agent.capabilities import CAPABILITIES

    cap = CAPABILITIES.get(rule.capability)
    label = cap.label if cap else rule.capability
    cs = await knowledge.begin(bid, "owner_command", f"Playbook: {label}", member_id=member_id)
    await knowledge.add(cs, "playbook_rule", "update", rule.id, {"effect": asked.effect})
    async with get_db().tenant_session(bid) as s:
        approval = await approvals.create(
            s,
            bid,
            "change_confirmation",
            {"change_set_id": str(cs.id)},
            {"summary": f"Playbook: {label}", "lines": [asked.readback(label)]},
            requested_by="rule_checker",
        )
    await messages.notify_owner(
        "owner.rule_checked",
        business_id=bid,
        facts={"rule_readback": asked.readback(label), "code": approval.short_code},
        context={
            "until_now": rule.readback(label),
            "differences": [d.model_dump() for d in result.differences],
            "shadowed_by": result.shadowed_by,
        },
        kind="rule_checked",
        dedupe_key=f"rulechecked:{rule.id}",
        priority=PriorityClass.P2,
        buttons={"approve": f"apv:{approval.id}:a", "reject": f"apv:{approval.id}:r"},
        ref={"approval_id": str(approval.id)},
    )
