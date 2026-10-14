"""`ImportWorkflow` (POC_05 §3.1, task queue `reflexo-main`, id `import-<run id>`).

start → extract → prepare (validate, match, draft change set) → ask the owner (approval + code) → wait for
`approval_decided` (24 h) → apply. Deterministic: all I/O happens in activities.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from reflexo.knowledge.api import imports as knowledge_imports  # summary_text: pure function
    from reflexo.workflows import import_activities as acts
    from reflexo.workflows import onboarding as onb

SHORT = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ImportFailed"])
REVIEW_TIMEOUT = timedelta(hours=24)


def _owner_message(err: ActivityError) -> str:
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type == "ImportFailed":
        return f"Couldn't import this file: {cause.message}"
    return "Sorry, something went wrong reading this file. Please send it again."


@workflow.defn(name="ImportWorkflow")
class ImportWorkflow:
    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None

    @workflow.signal(name="approval_decided")
    async def approval_decided(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @workflow.query(name="state")
    def state(self) -> dict[str, Any]:
        return {"decision": self._decision}

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        try:
            meta = await workflow.execute_activity(
                acts.import_start, inp, start_to_close_timeout=timedelta(seconds=10), retry_policy=SHORT
            )
            items = await workflow.execute_activity(
                acts.import_extract,
                args=[inp, meta],
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=RetryPolicy(maximum_attempts=2, non_retryable_error_types=["ImportFailed"]),
            )
            prepared = await workflow.execute_activity(
                acts.import_prepare,
                args=[inp, meta, items],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT,
            )
        except ActivityError as err:
            await self._finish(inp, "failed", _owner_message(err))
            return {"status": "failed"}

        if not prepared["change_set_id"]:
            await self._finish(inp, "cancelled", knowledge_imports.summary_text(meta.get("filename"), prepared, None))
            return {"status": "no_changes", "stats": prepared["stats"]}

        await workflow.execute_activity(
            acts.import_ask,
            args=[inp, meta, prepared],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=SHORT,
        )
        try:
            await workflow.wait_condition(lambda: self._decision is not None, timeout=REVIEW_TIMEOUT)
        except TimeoutError:
            await self._finish(inp, "cancelled", "The import review expired. Nothing was changed.")
            return {"status": "expired"}
        assert self._decision is not None
        if not self._decision.get("approve"):
            await self._finish(inp, "cancelled", "Cancelled. Nothing changed.")
            return {"status": "cancelled"}
        try:
            await workflow.execute_activity(
                acts.import_apply,
                args=[inp, prepared],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT,
            )
        except ActivityError as err:
            await self._finish(inp, "failed", _owner_message(err))
            return {"status": "failed"}
        if inp.get("mode") == "onboarding":  # continue the onboarding state machine (gap questions / preview)
            nxt = await workflow.execute_activity(
                onb.after_import, inp, start_to_close_timeout=timedelta(seconds=20), retry_policy=SHORT
            )
            if nxt["preview"]:
                await onb.run_preview(inp)
        return {"status": "committed", "stats": prepared["stats"]}

    async def _finish(self, inp: dict[str, Any], status: str, message: str) -> None:
        await workflow.execute_activity(
            acts.import_finish,
            args=[inp, status, message],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
