"""`AutomationBuildWorkflow` (POC_06 §4.1, id `build-<uuid>`): owner's words → definitions → validate (≤ 2 repair
rounds) → deterministic read-back → approval `automation_activation` → activate on YES.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import text

    import reflexo.approvals.api as approvals
    import reflexo.automations.api as automations
    import reflexo.gateway.api as gateway
    from reflexo.common.config import get_settings
    from reflexo.common.db import get_db
    from reflexo.common.enums import PipelineEventStatus
    from reflexo.common.xray import emit

MAX_REPAIRS = 2
DECISION_TIMEOUT = timedelta(hours=24)
RETRY = RetryPolicy(maximum_attempts=3)


async def _say(inp: dict[str, Any], key: str, blocks: list[dict[str, Any]]) -> None:
    business_id = UUID(inp["business_id"])
    async with get_db().tenant_session(business_id) as s:
        await gateway.outbound().enqueue(
            s,
            business_id=business_id,
            conversation_id=UUID(inp["reply_conversation_id"]),
            blocks=blocks,
            origin="system",
            idempotency_base=f"build:{inp['build_id']}:{key}",
        )


@activity.defn(name="build.draft")
async def build_draft(inp: dict[str, Any], previous: list[dict[str, Any]], feedback: list[str]) -> list[dict[str, Any]]:
    return await automations.builder.draft(
        UUID(inp["business_id"]), inp["description"], feedback=feedback or None, previous=previous or None
    )


@activity.defn(name="build.check")
async def build_check(definitions: list[dict[str, Any]]) -> dict[str, Any]:
    fixed, problems = automations.builder.check(definitions)
    return {"definitions": fixed, "problems": problems}


@activity.defn(name="build.request_activation")
async def request_activation(inp: dict[str, Any], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Store the drafts (inactive), send the read-back with a YES code."""
    business_id = UUID(inp["business_id"])
    member_id = UUID(inp["member_id"]) if inp.get("member_id") else None
    async with get_db().tenant_session(business_id) as s:
        existing = (
            await s.execute(
                text("""SELECT id, short_code, subject FROM approval_requests WHERE status = 'pending'
                         AND subject->>'workflow_id' = :w LIMIT 1"""),
                {"w": inp["workflow_id"]},
            )
        ).first()
    if existing is not None:  # a retried activity: same drafts, same code
        return {"code": existing.short_code, "automation_ids": existing.subject["automation_ids"]}
    installed = [
        await automations.install(
            business_id,
            raw,
            description_nl=inp["description"],
            member_id=member_id,
            activate=False,
            language=inp["language"],
        )
        for raw in definitions
    ]
    readback = automations.readback.readback(
        [i.definition for i in installed], inp["language"], demo_time_scale=get_settings().demo_time_scale
    )
    ids = [str(i.automation_id) for i in installed]
    async with get_db().tenant_session(business_id) as s:
        approval = await approvals.create(
            s,
            business_id,
            "automation_activation",
            {
                "automation_ids": ids,
                "workflow_id": inp["workflow_id"],
                "owner_conversation_id": inp["reply_conversation_id"],
            },
            {"summary": f"{len(ids)} automation{'s' if len(ids) > 1 else ''}", "lines": [readback]},
            requested_by=f"builder:{inp['build_id']}",
        )
    hi = inp["language"] == "hi-Latn"
    ask = (
        f"Lagaun? *YES {approval.short_code}* / NO {approval.short_code}"
        if hi
        else f"Activate? *YES {approval.short_code}* / NO"
    )
    text_body = f"{readback}\n\n{ask}"
    buttons = [
        {"id": f"apv:{approval.id}:a", "title": "✅ Lagao" if hi else "✅ Activate"},
        {"id": f"apv:{approval.id}:r", "title": "✖ Nahi" if hi else "✖ Cancel"},
    ]
    blocks: list[dict[str, Any]] = (
        [{"type": "buttons", "text": text_body, "options": buttons}]
        if len(text_body) <= 1024
        else [{"type": "text", "text": text_body}, {"type": "buttons", "text": ask, "options": buttons}]
    )
    await _say(inp, "readback", blocks)
    await emit("automation_build", PipelineEventStatus.OK, business_id=business_id, step="readback", count=len(ids))
    return {"code": approval.short_code, "automation_ids": ids}


@activity.defn(name="build.activate")
async def build_activate(inp: dict[str, Any], automation_ids: list[str]) -> None:
    await automations.activate(UUID(inp["business_id"]), [UUID(a) for a in automation_ids])
    hi = inp["language"] == "hi-Latn"
    msg = (
        "Active ✅ Kabhi bhi dekhne ke liye: 'automations dikhao'"
        if hi
        else "Active ✅ Ask 'show automations' anytime."
    )
    await _say(inp, "active", [{"type": "text", "text": msg}])
    await emit("automation_build", PipelineEventStatus.OK, business_id=UUID(inp["business_id"]), step="activated")


@activity.defn(name="build.finish")
async def build_finish(inp: dict[str, Any], automation_ids: list[str], message: str) -> None:
    """Rejected / expired / failed: drafts are archived, the owner is told what happened."""
    business_id = UUID(inp["business_id"])
    if automation_ids:
        async with get_db().tenant_session(business_id) as s:
            await s.execute(
                text("UPDATE automations SET status = 'archived' WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": automation_ids},
            )
            await s.execute(
                text("""UPDATE approval_requests SET status = 'cancelled'
                         WHERE status = 'pending' AND subject->>'workflow_id' = :w"""),
                {"w": inp["workflow_id"]},
            )
    await _say(inp, "finish", [{"type": "text", "text": message}])


BUILD_ACTIVITIES: list[Callable[..., Any]] = [
    build_draft,
    build_check,
    request_activation,
    build_activate,
    build_finish,
]


@workflow.defn(name="AutomationBuildWorkflow")
class AutomationBuildWorkflow:
    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None

    @workflow.signal(name="approval_decided")
    async def approval_decided(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        hi = inp["language"] == "hi-Latn"
        definitions: list[dict[str, Any]] = []
        problems: list[str] = []
        try:
            for _ in range(MAX_REPAIRS + 1):
                definitions = await workflow.execute_activity(
                    build_draft,
                    args=[inp, definitions, problems],
                    start_to_close_timeout=timedelta(seconds=90),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                checked = await workflow.execute_activity(
                    build_check, definitions, start_to_close_timeout=timedelta(seconds=10), retry_policy=RETRY
                )
                definitions, problems = checked["definitions"], checked["problems"]
                if not problems:
                    break
        except ActivityError:
            problems = ["model unavailable"]
        if problems:
            msg = (
                "Ye rule theek se samajh nahi paayi 🙏 Thoda aur detail mein batayiye?"
                if hi
                else "I couldn't turn that into a working rule. Could you say it in a bit more detail?"
            )
            await workflow.execute_activity(
                build_finish, args=[inp, [], msg], start_to_close_timeout=timedelta(seconds=10), retry_policy=RETRY
            )
            return {"status": "failed", "problems": problems}

        requested = await workflow.execute_activity(
            request_activation,
            args=[inp, definitions],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RETRY,
        )
        ids: list[str] = requested["automation_ids"]
        try:
            await workflow.wait_condition(lambda: self._decision is not None, timeout=DECISION_TIMEOUT)
        except TimeoutError:
            await workflow.execute_activity(
                build_finish,
                args=[
                    inp,
                    ids,
                    "Rules activate nahi kiye (24 ghante mein jawab nahi aaya)."
                    if hi
                    else "The rules were not activated (no answer in 24 h).",
                ],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RETRY,
            )
            return {"status": "expired"}
        assert self._decision is not None
        if not self._decision.get("approve"):
            await workflow.execute_activity(
                build_finish,
                args=[
                    inp,
                    ids,
                    "Theek hai, nahi lagaya. Kya badlun?" if hi else "OK, not activated. What should I change?",
                ],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RETRY,
            )
            return {"status": "rejected"}
        await workflow.execute_activity(
            build_activate, args=[inp, ids], start_to_close_timeout=timedelta(seconds=20), retry_policy=RETRY
        )
        return {"status": "active", "automation_ids": ids}
