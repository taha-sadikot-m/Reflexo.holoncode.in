"""Simulation Lab scenarios (POC_07 §3.3): scripted stories run server-side through the REAL pipeline.

A scenario is YAML in `config/scenarios/`. Customers are fresh simulated actors per run (no state leaks between runs);
the owner is the business's simulated owner device. Steps run in order; a failed `wait_for` / `assert` stops the run
and marks the step. Progress lives in memory (one API process) and is polled by the Lab page.

Step verbs: send_text, tap_option, pay_last_link, owner_answer_last_question, reply_with_last_short_code,
wait_for, assert, skip_waits, chaos, sleep_ms.
Conditions (wait_for / assert): replied, reply_contains, reply_has_options, owner_message_contains, order_status,
payment_status, payment_link_created, order_changed.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import text

import reflexo.commerce.api as commerce
from reflexo.admin import chaos, sim
from reflexo.common import ports
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import utcnow

log = get_logger(__name__)
SCENARIO_DIR = Path(__file__).resolve().parents[3] / "config" / "scenarios"
DEFAULT_TIMEOUT_S = 60.0
POLL_S = 0.7


class StepFailed(Exception):
    pass


@dataclass
class StepState:
    index: int
    label: str
    status: str = "pending"  # pending | running | ok | failed | skipped
    detail: str | None = None
    seconds: float | None = None


@dataclass
class ScenarioRun:
    id: str
    scenario_id: str
    title: str
    business_id: str
    status: str = "running"  # running | passed | failed | stopped
    steps: list[StepState] = field(default_factory=list)
    actors: dict[str, int] = field(default_factory=dict)  # customer name -> simulated actor number
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    finished_at: str | None = None


_runs: dict[str, ScenarioRun] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def load(scenario_id: str | None = None) -> list[dict[str, Any]]:
    out = []
    for f in sorted(SCENARIO_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if scenario_id is None or data.get("id") == scenario_id:
            out.append(data)
    return out


def catalogue() -> list[dict[str, Any]]:
    return [
        {"id": s["id"], "title": s.get("title", s["id"]), "covers": s.get("covers", ""), "steps": len(s["steps"])}
        for s in load()
    ]


def _label(step: dict[str, Any]) -> str:
    who = f"{step['actor']}: " if step.get("actor") else ""
    verb = next(k for k in step if k not in ("actor", "timeout_s"))
    value = step[verb]
    return f"{who}{verb.replace('_', ' ')} {value}".strip()


def new_run(scenario: dict[str, Any], business_id: UUID) -> ScenarioRun:
    """A run of `scenario` with fresh simulated customers (no state leaks between runs)."""
    customers = [name for name, a in (scenario.get("actors") or {}).items() if a.get("role") == "customer"]
    return ScenarioRun(
        id=new_id().hex,
        scenario_id=scenario["id"],
        title=scenario.get("title", scenario["id"]),
        business_id=str(business_id),
        steps=[StepState(i, _label(s)) for i, s in enumerate(scenario["steps"])],
        actors={name: 1000 + secrets.randbelow(8999) for name in customers},
    )


async def execute(run: ScenarioRun, scenario: dict[str, Any], business_id: UUID) -> None:
    """Run every step in order in this task (the benchmark awaits it; the Lab runs it in the background)."""
    await _execute(run, scenario, business_id)


def start(scenario_id: str, business_id: UUID) -> ScenarioRun:
    found = load(scenario_id)
    if not found:
        raise LookupError(f"unknown scenario {scenario_id}")
    scenario = found[0]
    run = new_run(scenario, business_id)
    _runs[run.id] = run
    task = asyncio.create_task(_execute(run, scenario, business_id), name=f"scenario:{run.id}")
    _tasks[run.id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run.id, None))
    return run


def get(run_id: str) -> dict[str, Any] | None:
    run = _runs.get(run_id)
    return asdict(run) if run else None


def recent(limit: int = 10) -> list[dict[str, Any]]:
    return [asdict(r) for r in sorted(_runs.values(), key=lambda r: r.started_at, reverse=True)[:limit]]


def stop(run_id: str) -> bool:
    task = _tasks.get(run_id)
    if task is None:
        return False
    task.cancel()
    return True


async def _execute(run: ScenarioRun, scenario: dict[str, Any], business_id: UUID) -> None:
    ctx = _Context(business_id, run)
    try:
        for state, step in zip(run.steps, scenario["steps"], strict=True):
            state.status = "running"
            started = time.monotonic()
            try:
                state.detail = await ctx.do(step)
                state.status = "ok"
            except StepFailed as exc:
                state.status, state.detail = "failed", str(exc)
                run.status = "failed"
                return
            finally:
                state.seconds = round(time.monotonic() - started, 1)
        run.status = "passed"
    except asyncio.CancelledError:
        run.status = "stopped"
        for s in run.steps:
            if s.status in ("pending", "running"):
                s.status = "skipped"
    except Exception as exc:  # a broken step must not leave the run "running" forever
        log.exception("scenario_failed", scenario=run.scenario_id)
        run.status = "failed"
        current = next((s for s in run.steps if s.status == "running"), None)
        if current:
            current.status, current.detail = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if scenario.get("cleanup", True):
            await _cleanup(run, business_id, keep_learned=bool(scenario.get("keep_learned", False)))
        run.finished_at = utcnow().isoformat()


async def _cleanup(run: ScenarioRun, business_id: UUID, *, keep_learned: bool) -> None:
    """Cancel the run's still-live orders so simulated stories never hold real capacity (live 12 Oct: Lab orders
    filled the next day's cake limit and real customers were told 'fully booked'), and archive playbook rules the
    run's simulated owner taught — unless the scenario keeps them (e.g. to show the Playbook page in a demo)."""
    keys = [sim.actor_wa_id(a) for a in run.actors.values()]
    async with get_db().tenant_session(business_id) as s:
        if not keep_learned:  # what the simulated owner taught must not stay in the real business's knowledge
            await s.execute(
                text("""UPDATE qa_entries SET status = 'archived'
                        WHERE status = 'active' AND id IN (
                          SELECT q.qa_entry_id FROM owner_questions q JOIN conversations c ON c.id = q.conversation_id
                          WHERE c.simulated AND c.thread_key = ANY(:k) AND q.qa_entry_id IS NOT NULL)"""),
                {"k": keys},
            )
            await s.execute(
                text("""UPDATE playbook_rules SET status = 'archived', updated_at = now()
                        WHERE status = 'active' AND source_ref IN (
                          SELECT a.id FROM approval_requests a JOIN conversations c
                            ON c.id = CAST(a.subject->>'conversation_id' AS uuid)
                          WHERE c.simulated AND c.thread_key = ANY(:k))"""),
                {"k": keys},
            )
        ids = (
            (
                await s.execute(
                    text("""SELECT o.id FROM orders o JOIN conversations c ON c.customer_id = o.customer_id
                        WHERE c.simulated AND c.thread_key = ANY(:k)
                          AND o.status IN ('draft', 'awaiting_payment', 'confirmed', 'preparing')"""),
                    {"k": keys},
                )
            )
            .scalars()
            .all()
        )
    for order_id in set(ids):
        try:
            await commerce.cancel_order(business_id, order_id, by="system", reason="lab cleanup", notify_owner=False)
        except commerce.CommerceError:
            log.info("scenario_cleanup_skip", order_id=str(order_id))


class _Context:
    def __init__(self, business_id: UUID, run: ScenarioRun) -> None:
        self.business_id = business_id
        self.run = run
        self.last_actor: str | None = None
        # waits only count messages that arrive after the latest action by anyone (a send, tap, answer or payment):
        # "the reply to what just happened", never an older one (the owner device is shared across runs, too)
        self.last_action_at: datetime = utcnow()
        self.order_before: dict[str, Any] = {}  # actor -> their latest order as it was when they last spoke

    def actor(self, step: dict[str, Any]) -> str:
        name = step.get("actor") or self.last_actor
        if not name:
            raise StepFailed("no actor for this step")
        return str(name)

    # ------------------------------------------------------------ verbs

    async def do(self, step: dict[str, Any]) -> str | None:
        verb = next(k for k in step if k not in ("actor", "timeout_s"))
        arg = step[verb]
        timeout = float(step.get("timeout_s", DEFAULT_TIMEOUT_S))
        if verb == "send_text":
            return await self.say(self.actor(step), text_=str(arg))
        if verb == "tap_option":
            return await self.tap(self.actor(step), str(arg))
        if verb == "pay_last_link":
            return await self.pay(self.actor(step) if isinstance(arg, bool) or arg is None else str(arg))
        if verb == "owner_answer_last_question":
            code = await self.owner_code(r"Question (\d{4})")
            return await self.say("owner", text_=f"{code} {arg}")
        if verb == "reply_with_last_short_code":
            code = await self.owner_code(r"YES (\d{4})")
            return await self.say("owner", text_=f"{arg} {code}")
        if verb in ("wait_for", "assert"):
            return await self.until(step, dict(arg), timeout if verb == "wait_for" else 3.0)
        if verb == "skip_waits":
            return await self.skip_waits()
        if verb == "chaos":
            result = await chaos.run(str(arg), self.business_id)
            if not result.ok:
                raise StepFailed(f"chaos {arg} not as expected: {result.checks}")
            return "verified"
        if verb == "sleep_ms":
            await asyncio.sleep(int(arg) / 1000)
            return None
        raise StepFailed(f"unknown step verb {verb}")

    async def say(self, who: str, *, text_: str | None = None, option: tuple[str, str] | None = None) -> str:
        self.last_action_at = utcnow()
        if who == "owner":
            if option:
                await sim.owner_says(self.business_id, option_id=option[0], option_title=option[1])
            else:
                await sim.owner_says(self.business_id, text_=text_)
        else:
            self.last_actor = who
            actor = self.run.actors[who]
            order = await self.latest_order(who)
            self.order_before[who] = dict(order._mapping) if order else {}
            if option:
                await sim.customer_says(self.business_id, actor, option_id=option[0], option_title=option[1], name=who)
            else:
                await sim.customer_says(self.business_id, actor, text_=text_, name=who)
        return f"sent: {option[1] if option else text_}"

    async def thread(self, who: str) -> dict[str, Any]:
        if who == "owner":
            return await sim.owner_thread(self.business_id)
        return await sim.thread(self.business_id, self.run.actors[who])

    async def replies(self, who: str) -> list[dict[str, Any]]:
        items = (await self.thread(who))["items"]
        return [
            i["payload"]
            for i in items
            if i["from"] == "business" and datetime.fromisoformat(i["at"]) >= self.last_action_at
        ]

    async def tap(self, who: str, title: str) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for payload in reversed(await self.replies(who)):
                for oid, otitle in sim.payload_options(payload):
                    if title.lower() in otitle.lower():
                        return await self.say(who, option=(oid, otitle))
            await asyncio.sleep(POLL_S)
        raise StepFailed(f"no option like '{title}' for {who}")

    async def pay(self, who: str) -> str:
        links = (await self.thread(who)).get("payment_links") or []
        live = next((link for link in links if link["status"] == "created"), None)
        if live is None:
            raise StepFailed(f"{who} has no unpaid payment link")
        self.last_action_at = utcnow()
        status = await sim.pay_link(self.business_id, UUID(live["id"]))
        return f"paid {live['order_number']} (webhook {status})"

    async def owner_code(self, pattern: str) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for payload in reversed((await self.thread("owner"))["items"]):
                if payload["from"] == "business" and (m := re.search(pattern, sim.payload_text(payload["payload"]))):
                    return m.group(1)
            await asyncio.sleep(POLL_S)
        raise StepFailed(f"no owner message matching {pattern}")

    async def skip_waits(self) -> str:
        async with get_db().tenant_session(self.business_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT workflow_id FROM automation_runs WHERE status IN ('running', 'waiting') "
                        "AND workflow_id IS NOT NULL"
                    )
                )
            ).all()
        port = ports.registry.require(WorkflowPort)  # type: ignore[type-abstract]
        sent = [await port.signal(r.workflow_id, "skip_wait", {}) for r in rows]
        return f"fast-forwarded {sum(sent)} run(s)"

    # ------------------------------------------------------------ conditions

    async def until(self, step: dict[str, Any], cond: dict[str, Any], within_s: float) -> str:
        deadline = time.monotonic() + within_s
        last = ""
        while True:
            ok, last = await self.holds(step, cond)
            if ok:
                return last
            if time.monotonic() > deadline:
                raise StepFailed(f"not met after {within_s:.0f}s: {cond} (last seen: {last[:160]})")
            await asyncio.sleep(POLL_S)

    async def holds(self, step: dict[str, Any], cond: dict[str, Any]) -> tuple[bool, str]:
        if "owner_message_contains" in cond:
            texts = [sim.payload_text(p) for p in await self.replies("owner")]
            needle = str(cond["owner_message_contains"]).lower()
            return any(needle in t.lower() for t in texts), (texts[-1] if texts else "")
        who = self.actor(step)
        if "replied" in cond:  # the business answered this actor (or the owner) since the latest action
            texts = [sim.payload_text(p) for p in await self.replies(who)]
            return bool(texts) == bool(cond["replied"]), (texts[-1] if texts else "")
        if "reply_contains" in cond:
            texts = [sim.payload_text(p) for p in await self.replies(who)]
            needles = cond["reply_contains"] if isinstance(cond["reply_contains"], list) else [cond["reply_contains"]]
            hit = any(str(n).lower() in t.lower() for t in texts for n in needles)
            return hit, (texts[-1] if texts else "")
        if "reply_has_options" in cond:
            return any(sim.payload_options(p) for p in await self.replies(who)), ""
        order = await self.latest_order(who)
        if order is None:
            return False, "no order yet"
        seen = f"{order.order_number}: {order.status} / {order.payment_status} / {order.delivery_date}"
        if "order_changed" in cond:  # the business outcome, not the phrasing of a reply
            field = str(cond["order_changed"])
            before = self.order_before.get(who, {})
            return before.get("order_number") == order.order_number and before.get(field) != getattr(order, field), seen
        if "order_status" in cond:
            return str(order.status) == cond["order_status"], seen
        if "payment_status" in cond:
            return str(order.payment_status) == cond["payment_status"], seen
        if "payment_link_created" in cond:
            links = (await self.thread(who)).get("payment_links") or []
            return bool(links), seen
        raise StepFailed(f"unknown condition {cond}")

    async def latest_order(self, who: str) -> Any:
        async with get_db().tenant_session(self.business_id) as s:
            return (
                await s.execute(
                    text("""SELECT o.order_number, o.status, o.payment_status, o.delivery_date, o.delivery_slot,
                                   o.address FROM orders o
                            JOIN conversations c ON c.customer_id = o.customer_id
                            WHERE c.thread_key = :t AND c.simulated ORDER BY o.created_at DESC LIMIT 1"""),
                    {"t": sim.actor_wa_id(self.run.actors[who])},
                )
            ).first()
