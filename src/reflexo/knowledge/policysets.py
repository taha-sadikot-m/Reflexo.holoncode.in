"""The owner's playbook rules compiled to Cedar (BETA_04 §7.4; BETA_02 §4.16).

`rebuild` runs in the same transaction as every change set that touches a playbook rule (undo included), so a
decision never sees rules and compiled sets that disagree. A new version is written only when the rules of a
layer changed (`source_hash`); older versions stay as the basis of past decisions.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.policy.api as policy
from reflexo.common.db import get_db
from reflexo.common.logging import get_logger

log = get_logger(__name__)
LAYERS: tuple[policy.RuleLayer, ...] = ("customer", "shop")


def rule_of(row: Any) -> policy.Rule:
    return policy.Rule(
        id=row.id,
        capability=row.capability,
        effect=row.effect,
        conditions=[policy.Condition.model_validate(c) for c in (row.conditions or [])],
        says=row.says,
        scope=str(row.scope),
        customer_id=row.customer_id,
        source=row.source,
    )


async def active_rules(s: AsyncSession) -> list[policy.Rule]:
    rows = (
        await s.execute(
            text("""SELECT id, capability, effect, conditions, says, scope, customer_id, source
                    FROM playbook_rules WHERE status = 'active' ORDER BY created_at, id""")
        )
    ).all()
    return [rule_of(r) for r in rows]


async def rebuild(s: AsyncSession, business_id: UUID, *, analysis: dict[str, Any] | None = None) -> dict[str, int]:
    """Compile both rule layers; write a new version of each layer whose rules changed. Returns the versions."""
    rules = await active_rules(s)
    versions: dict[str, int] = {}
    for layer in LAYERS:
        compiled = policy.compile_rules(rules, layer=layer)
        latest = (
            await s.execute(
                text("""SELECT version, source_hash FROM policy_sets WHERE business_id = :b AND layer = :l
                        ORDER BY version DESC LIMIT 1"""),
                {"b": business_id, "l": layer},
            )
        ).first()
        if latest is not None and bytes(latest.source_hash) == compiled.source_hash:
            versions[layer] = int(latest.version)
            continue
        version = (int(latest.version) if latest else 0) + 1
        await s.execute(
            text("""INSERT INTO policy_sets (business_id, layer, version, cedar_text, schema_fragment, source_hash,
                                             analysis)
                    VALUES (:b, :l, :v, :t, '', :h, CAST(:a AS jsonb))"""),
            {
                "b": business_id,
                "l": layer,
                "v": version,
                "t": compiled.cedar_text,
                "h": compiled.source_hash,
                "a": None if analysis is None else json.dumps(analysis, default=str),
            },
        )
        versions[layer] = version
        log.info("policy_set_compiled", business_id=str(business_id), layer=layer, version=version)
    return versions


async def current(business_id: UUID) -> dict[str, int | None]:
    """The compiled version of each layer that matches the active rules right now. Change sets rebuild in their
    own transaction; this also catches any other write to `playbook_rules` (an operator fix, a script), so a
    decision never runs on rules that are no longer there."""
    async with get_db().tenant_session(business_id) as s:
        rules = await active_rules(s)
        rows = (
            await s.execute(
                text("""SELECT DISTINCT ON (layer) layer, version, source_hash FROM policy_sets
                        WHERE business_id = :b ORDER BY layer, version DESC"""),
                {"b": business_id},
            )
        ).all()
        latest = {r.layer: r for r in rows}
        versions: dict[str, int | None] = {}
        for layer in LAYERS:
            mine = [r for r in rules if r.scope == ("customer" if layer == "customer" else "global")]
            row = latest.get(layer)
            if row is None and not mine:
                versions[layer] = None
            elif row is not None and bytes(row.source_hash) == policy.source_hash(mine):
                versions[layer] = int(row.version)
            else:
                log.warning("policy_set_stale", business_id=str(business_id), layer=layer)
                return dict(await rebuild(s, business_id))
    return versions


async def text_of(business_id: UUID, layer: str, version: int) -> tuple[str, str]:
    """(cedar_text, schema_fragment) of one compiled version."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("""SELECT cedar_text, schema_fragment FROM policy_sets
                        WHERE business_id = :b AND layer = :l AND version = :v"""),
                {"b": business_id, "l": layer, "v": version},
            )
        ).one()
    return str(row.cedar_text), str(row.schema_fragment)


async def record_analysis(business_id: UUID, analysis: dict[str, Any]) -> None:
    """The last checker result on the current version of each layer."""
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""UPDATE policy_sets p SET analysis = CAST(:a AS jsonb)
                    WHERE p.business_id = :b AND p.version = (SELECT max(version) FROM policy_sets q
                                                              WHERE q.business_id = p.business_id
                                                                AND q.layer = p.layer)"""),
            {"b": business_id, "a": json.dumps(analysis, default=str)},
        )
