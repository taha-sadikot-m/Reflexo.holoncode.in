"""Automation rows (POC_06 §4.1 step 6): validated + compiled definitions become versioned, activatable rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.automations.readback import describe
from reflexo.automations.schema import AutomationDefinition
from reflexo.automations.validator import ValidationReport, compile_definition, parse, validate
from reflexo.common.db import get_db
from reflexo.common.ids import new_id


class InvalidDefinition(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        super().__init__("; ".join(f"{f.code}: {f.message}" for f in [*report.errors, *report.owner_questions]))
        self.report = report


@dataclass(frozen=True)
class Installed:
    automation_id: UUID
    version_id: UUID
    executor: str
    definition: AutomationDefinition


def checked(raw: dict[str, Any]) -> tuple[AutomationDefinition, ValidationReport]:
    d, report = parse(raw)
    if d is None:
        raise InvalidDefinition(report)
    report = validate(d)
    if not report.ok:
        raise InvalidDefinition(report)
    return d, report


async def install(
    business_id: UUID,
    raw: dict[str, Any],
    *,
    description_nl: str,
    member_id: UUID | None = None,
    activate: bool = True,
    language: str = "hi-Latn",
) -> Installed:
    """Validate, compile and store a new automation (version 1). `activate` sets it live at once."""
    d, report = checked(raw)
    executor, index = compile_definition(d)
    automation_id, version_id = new_id(), new_id()
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO automations (id, business_id, name, description_nl, status, current_version_id,
                                             created_by_member_id)
                    VALUES (:id, :b, :n, :nl, CAST(:st AS automation_status), :v, :m)"""),
            {
                "id": automation_id,
                "b": business_id,
                "n": d.name,
                "nl": description_nl,
                "st": "active" if activate else "draft",
                "v": version_id,
                "m": member_id,
            },
        )
        await s.execute(
            text("""INSERT INTO automation_versions (id, business_id, automation_id, version, definition, executor,
                                                     trigger_index, readback, validation, activated_at)
                    VALUES (:id, :b, :a, 1, CAST(:d AS jsonb), CAST(:ex AS automation_executor),
                            CAST(:ti AS jsonb), :rb, CAST(:val AS jsonb), CASE WHEN :act THEN now() END)"""),
            {
                "id": version_id,
                "b": business_id,
                "a": automation_id,
                "d": d.model_dump_json(by_alias=True),
                "ex": executor,
                "ti": index.model_dump_json(),
                "rb": describe(d, language),
                "val": report.model_dump_json(),
                "act": activate,
            },
        )
    return Installed(automation_id, version_id, executor, d)


async def activate(business_id: UUID, automation_ids: list[UUID]) -> None:
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""UPDATE automations SET status = 'active', updated_at = now() WHERE id = ANY(:ids)"""),
            {"ids": automation_ids},
        )
        await s.execute(
            text("""UPDATE automation_versions SET activated_at = now()
                    WHERE automation_id = ANY(:ids) AND activated_at IS NULL"""),
            {"ids": automation_ids},
        )


@dataclass(frozen=True)
class ActiveVersion:
    automation_id: UUID
    version_id: UUID
    name: str
    priority: int
    executor: str
    definition: AutomationDefinition
    trigger_index: dict[str, Any]


async def active_versions(business_id: UUID, *, executor: str | None = None) -> list[ActiveVersion]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT a.id, a.name, a.priority, v.id AS vid, v.executor, v.definition, v.trigger_index
                        FROM automations a JOIN automation_versions v ON v.id = a.current_version_id
                        WHERE a.status = 'active' AND (CAST(:ex AS text) IS NULL OR v.executor::text = :ex)
                        ORDER BY a.priority, a.created_at"""),
                {"ex": executor},
            )
        ).all()
    return [
        ActiveVersion(
            r.id,
            r.vid,
            r.name,
            r.priority,
            str(r.executor),
            AutomationDefinition.model_validate(r.definition),
            dict(r.trigger_index),
        )
        for r in rows
    ]


def to_json(d: AutomationDefinition) -> str:
    return json.dumps(d.model_dump(by_alias=True), ensure_ascii=False)
