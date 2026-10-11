"""`AutomationRunWorkflow` (POC_06 §7.2): the durable interpreter, id `auto-<version>-<instance key>`.

Deterministic: workflow code only sequences; CEL, sends and DB writes are activities. Durations are multiplied by
the demo time scale captured at start (1 day ≈ 60 s in the demo); `skip_wait` fast-forwards the current wait.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import text

    import reflexo.automations.api as automations
    from reflexo.automations.api import iso_seconds
    from reflexo.automations.api import runtime as rt
    from reflexo.common.db import get_db
    from reflexo.common.ids import new_id

STEP_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2), backoff_coefficient=2)
SHORT = RetryPolicy(maximum_attempts=3)
TIMEOUT = timedelta(seconds=30)


@activity.defn(name="auto.load")
async def run_load(inp: dict[str, Any]) -> dict[str, Any]:
    _, d, _ = await rt.load_run(UUID(inp["business_id"]), UUID(inp["run_id"]))
    return {"definition": d.model_dump(by_alias=True)}


@activity.defn(name="auto.step")
async def run_step(inp: dict[str, Any], step_id: str, variables: dict[str, Any]) -> dict[str, Any]:
    business_id = UUID(inp["business_id"])
    ctx, d, _ = await rt.load_run(business_id, UUID(inp["run_id"]))
    ctx.variables = await rt.refresh_order(business_id, variables)
    return {"next": await rt.execute_step(ctx, d, step_id)}


@activity.defn(name="auto.until")
async def run_until(inp: dict[str, Any], expr: str, variables: dict[str, Any], now_iso: str) -> str:
    ctx, _, _ = await rt.load_run(UUID(inp["business_id"]), UUID(inp["run_id"]))
    target = automations.cel.evaluate(expr, variables, tz=ctx.tz, now=datetime.fromisoformat(now_iso))
    if not isinstance(target, datetime):
        raise ValueError(f"`{expr}` is not a timestamp")
    return target.isoformat()


@activity.defn(name="auto.register_wait")
async def register_wait(inp: dict[str, Any], step: dict[str, Any], variables: dict[str, Any], expires_iso: str) -> str:
    """The router delivers matching events: by customer for replies, by order for payments/status."""
    business_id = UUID(inp["business_id"])
    key = (
        (variables.get("customer") or {}).get("id")
        if step["event"] == "message_received"
        else (variables.get("order") or {}).get("id")
    )
    if not key:
        raise ValueError("nothing to correlate the wait with")
    wait_id = new_id()
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO automation_waits (id, business_id, run_id, workflow_id, event_type, correlation_key,
                                                  match_cel, consume, expires_at)
                    VALUES (:id, :b, :r, :w, :t, :k, :m, :c, :exp)"""),
            {
                "id": wait_id,
                "b": business_id,
                "r": UUID(inp["run_id"]),
                "w": activity.info().workflow_id,
                "t": step["event"],
                "k": str(key),
                "m": step.get("match") or "true",
                "c": bool(step.get("consume", True)),
                "exp": datetime.fromisoformat(expires_iso),
            },
        )
        await s.execute(text("UPDATE automation_runs SET status = 'waiting' WHERE id = :r"), {"r": UUID(inp["run_id"])})
    return str(wait_id)


@activity.defn(name="auto.clear_wait")
async def clear_wait(inp: dict[str, Any], wait_id: str, status: str) -> None:
    async with get_db().tenant_session(UUID(inp["business_id"])) as s:
        await s.execute(
            text("UPDATE automation_waits SET status = :st WHERE id = :id AND status = 'waiting'"),
            {"st": status, "id": UUID(wait_id)},
        )
        await s.execute(
            text("UPDATE automation_runs SET status = 'running' WHERE id = :r AND status = 'waiting'"),
            {"r": UUID(inp["run_id"])},
        )


@activity.defn(name="auto.finish")
async def run_finish(inp: dict[str, Any], status: str, error: str | None) -> None:
    await rt.finish_run(UUID(inp["business_id"]), UUID(inp["run_id"]), status, error)


RUN_ACTIVITIES: list[Callable[..., Any]] = [run_load, run_step, run_until, register_wait, clear_wait, run_finish]


@workflow.defn(name="AutomationRunWorkflow")
class AutomationRunWorkflow:
    def __init__(self) -> None:
        self._inbox: list[dict[str, Any]] = []
        self._skip = False

    @workflow.signal(name="event")
    async def event(self, payload: dict[str, Any]) -> None:
        self._inbox.append(payload)

    @workflow.signal(name="skip_wait")
    async def skip_wait(self) -> None:
        """Admin fast-forward (demo): end the current wait now."""
        self._skip = True

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        scale = float(inp.get("time_scale") or 1.0)
        variables: dict[str, Any] = inp["variables"]
        try:
            loaded = await workflow.execute_activity(run_load, inp, start_to_close_timeout=TIMEOUT, retry_policy=SHORT)
            steps = {s["id"]: s for s in loaded["definition"]["steps"]}
            step_id: str | None = loaded["definition"]["steps"][0]["id"]
            while step_id:
                step = steps[step_id]
                kind = step["type"]
                if kind == "wait":
                    if step.get("duration"):
                        seconds = iso_seconds(step["duration"])
                    else:
                        target = await workflow.execute_activity(
                            run_until,
                            args=[inp, step["until"], variables, workflow.now().isoformat()],
                            start_to_close_timeout=TIMEOUT,
                            retry_policy=SHORT,
                        )
                        seconds = (datetime.fromisoformat(target) - workflow.now()).total_seconds()
                    await self._sleep(seconds * scale)
                    step_id = step.get("next")
                elif kind == "wait_for_event":
                    seconds = iso_seconds(step["timeout"]) * scale
                    expires = workflow.now() + timedelta(seconds=seconds + 60)
                    wait_id = await workflow.execute_activity(
                        register_wait,
                        args=[inp, step, variables, expires.isoformat()],
                        start_to_close_timeout=TIMEOUT,
                        retry_policy=SHORT,
                    )
                    got = await self._wait_for(wait_id, seconds)
                    await workflow.execute_activity(
                        clear_wait,
                        args=[inp, wait_id, "matched" if got else "expired"],
                        start_to_close_timeout=TIMEOUT,
                        retry_policy=SHORT,
                    )
                    if got is not None:
                        variables = {**variables, "event": got.get("event", {})}
                        step_id = step.get("next")
                    else:
                        step_id = step.get("on_timeout")
                else:
                    result = await workflow.execute_activity(
                        run_step,
                        args=[inp, step_id, variables],
                        start_to_close_timeout=TIMEOUT,
                        retry_policy=STEP_RETRY,
                    )
                    step_id = result["next"]
        except ActivityError as err:
            await workflow.execute_activity(
                run_finish,
                args=[inp, "failed", str(err.cause or err)[:500]],
                start_to_close_timeout=TIMEOUT,
                retry_policy=SHORT,
            )
            return {"status": "failed"}
        await workflow.execute_activity(
            run_finish, args=[inp, "completed", None], start_to_close_timeout=TIMEOUT, retry_policy=SHORT
        )
        return {"status": "completed"}

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            try:
                await workflow.wait_condition(lambda: self._skip, timeout=timedelta(seconds=seconds))
            except TimeoutError:
                pass
        self._skip = False

    async def _wait_for(self, wait_id: str, seconds: float) -> dict[str, Any] | None:
        def arrived() -> dict[str, Any] | None:
            return next((e for e in self._inbox if e.get("wait_id") == wait_id), None)

        if seconds > 0 and arrived() is None:
            try:
                await workflow.wait_condition(
                    lambda: arrived() is not None or self._skip, timeout=timedelta(seconds=seconds)
                )
            except TimeoutError:
                pass
        self._skip = False
        return arrived()
