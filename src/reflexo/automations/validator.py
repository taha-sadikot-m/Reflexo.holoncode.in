"""Validation and compilation (POC_06 §2–3). Deterministic: nothing activates unless this passes.

`validate(definition)` → ValidationReport{errors, owner_questions, warnings}; `compile_definition` picks the
executor (guard / instant / durable) and builds the router's trigger index.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from reflexo.automations import cel
from reflexo.automations.schema import (
    DURABLE_STEPS,
    GUARD_STEPS,
    ApprovalStep,
    AutomationDefinition,
    BranchStep,
    ClaimOnceStep,
    CounterLimitStep,
    DenyStep,
    LlmClassifyStep,
    LlmGenerateStep,
    NotifyOwnerStep,
    SendStep,
    UpdateOrderStep,
    WaitForEventStep,
    WaitStep,
    iso_seconds,
)

DAY_S = 24 * 3600
MAX_DURATION_S = 30 * DAY_S
_MONEY_FIELDS = re.compile(r"price|amount|total|discount|paise|rupee", re.IGNORECASE)
_LITERAL_MONEY = re.compile(r"(₹|\brs\.?|\binr)\s*\d", re.IGNORECASE)
_UNTIL_TIME = re.compile(r"\.at\(\s*['\"](\d{1,2}):(\d{2})['\"]\s*\)")
_UNTIL_BEFORE = re.compile(r"-\s*duration\(\s*['\"](\d+)h['\"]\s*\)")
EVENT_VARS = {  # the awaited event inside wait_for_event.match
    "message_received": {"type": "message_received", "text": "haan confirm", "customer_id": "c", "channel": "whatsapp"},
    "payment_captured": {"type": "payment_captured", "payload": {"order_id": "00000000-0000-0000-0000-000000000001"}},
    "order_status_changed": {"type": "order_status_changed", "payload": {"to_status": "out_for_delivery"}},
}


class Finding(BaseModel):
    code: str
    message: str
    step: str | None = None
    options: list[str] = []


class ValidationReport(BaseModel):
    errors: list[Finding] = Field(default_factory=list)
    owner_questions: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.owner_questions


class TriggerIndex(BaseModel):
    type: str
    channels: list[str] = []
    keywords: list[str] = []
    post_id: str | None = None
    to_status: list[str] = []
    guard: bool = False


Executor = Literal["guard", "instant", "durable"]


def parse(raw: dict[str, Any]) -> tuple[AutomationDefinition | None, ValidationReport]:
    report = ValidationReport()
    try:
        return AutomationDefinition.model_validate(raw), report
    except ValidationError as exc:
        for e in exc.errors()[:10]:
            report.errors.append(Finding(code="V_SCHEMA", message=f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"))
        return None, report


def validate(d: AutomationDefinition) -> ValidationReport:
    r = ValidationReport()
    ids = [s.id for s in d.steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        r.errors.append(Finding(code="V_SCHEMA", message=f"duplicate step ids: {sorted(dupes)}"))
    known = set(ids)
    for s in d.steps:
        for target in d.successors(s):
            if target not in known:
                r.errors.append(Finding(code="V_SCHEMA", message=f"points to unknown step '{target}'", step=s.id))
    if r.errors:
        return r
    _graph(d, r)
    _types(d, r)
    _shape(d, r)
    _money(d, r)
    _window_and_quiet(d, r)
    if len(d.steps) > d.limits.max_steps:
        r.errors.append(Finding(code="V_LIMITS", message=f"{len(d.steps)} steps (max {d.limits.max_steps})"))
    if _worst_case_s(d) > MAX_DURATION_S:
        r.errors.append(Finding(code="V_LIMITS", message="longest path waits more than 30 days"))
    if d.trigger.type in ("message_received", "comment_received") and any(
        s.type in ("llm_classify", "llm_generate") for s in d.steps
    ):
        r.warnings.append(Finding(code="V_SEMANTIC_COST", message="runs an AI step on every matching message"))
    return r


def _graph(d: AutomationDefinition, r: ValidationReport) -> None:
    seen, queue = {d.steps[0].id}, deque([d.steps[0].id])
    while queue:
        for nxt in d.successors(d.step(queue.popleft())):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    for s in d.steps:
        if s.id not in seen:
            r.errors.append(Finding(code="V_UNREACHABLE", message="never reached", step=s.id))
    state: dict[str, int] = {}  # 1 = on stack, 2 = done

    def visit(sid: str) -> bool:
        state[sid] = 1
        for nxt in d.successors(d.step(sid)):
            if state.get(nxt) == 1 or (state.get(nxt) is None and visit(nxt)):
                return True
        state[sid] = 2
        return False

    if visit(d.steps[0].id):
        r.errors.append(Finding(code="V_CYCLE", message="steps loop back on themselves (not allowed)"))


def _expressions(d: AutomationDefinition) -> list[tuple[str | None, str, dict[str, Any] | None]]:
    """(step id, expression, extra variables) for every CEL expression and template in the definition."""
    out: list[tuple[str | None, str, dict[str, Any] | None]] = []

    def templates(step: str | None, text_: str | None) -> None:
        for expr in cel.expressions_in(text_ or ""):
            out.append((step, expr, None))

    if d.trigger.cel:
        out.append((None, d.trigger.cel, None))
    templates(None, d.instance_key)
    for s in d.steps:
        if isinstance(s, SendStep):
            templates(s.id, s.text)
            for block in s.blocks or []:
                for v in block.values():
                    if isinstance(v, str):
                        templates(s.id, v)
            for v in s.template_fallback.vars if s.template_fallback else []:
                templates(s.id, v)
        elif isinstance(s, WaitStep) and s.until:
            out.append((s.id, s.until, None))
        elif isinstance(s, WaitForEventStep):
            out.append((s.id, s.match, {"event": EVENT_VARS[s.event]}))
        elif isinstance(s, BranchStep):
            out.append((s.id, s.cel, None))
        elif isinstance(s, CounterLimitStep):
            templates(s.id, s.key)
            out.append((s.id, s.amount, None))
        elif isinstance(s, ClaimOnceStep):
            templates(s.id, s.key)
        elif isinstance(s, DenyStep):
            templates(s.id, s.reason)
        elif isinstance(s, NotifyOwnerStep):
            templates(s.id, s.text)
        elif isinstance(s, ApprovalStep):
            templates(s.id, s.preview)
        elif isinstance(s, LlmClassifyStep | LlmGenerateStep):
            templates(s.id, s.input)
    return out


def _types(d: AutomationDefinition, r: ValidationReport) -> None:
    base = cel.sample_variables(d.trigger.type)
    base["vars"] = {s.output_var: "label" for s in d.steps if isinstance(s, LlmClassifyStep | LlmGenerateStep)}
    for step, expr, extra in _expressions(d):
        try:
            cel.evaluate(expr, {**base, **(extra or {})})
        except cel.CelError as exc:
            r.errors.append(Finding(code="V_TYPES", message=str(exc), step=step))


def _shape(d: AutomationDefinition, r: ValidationReport) -> None:
    if d.kind == "guard":
        bad = [s.id for s in d.steps if s.type not in GUARD_STEPS]
        if bad or d.trigger.type != "order_confirming":
            r.errors.append(
                Finding(
                    code="V_GUARD_SHAPE", message="guards use only counter_limit/branch/deny/end on order_confirming"
                )
            )
        if not any(s.type == "deny" for s in d.steps):
            r.errors.append(Finding(code="V_GUARD_SHAPE", message="a guard needs a deny step"))
    elif d.trigger.type == "order_confirming":
        r.errors.append(Finding(code="V_GUARD_SHAPE", message="order_confirming is only for kind=guard"))
    if d.trigger.type == "schedule" and not d.trigger.cron:
        r.errors.append(Finding(code="V_SCHEMA", message="a schedule trigger needs cron"))


def _money(d: AutomationDefinition, r: ValidationReport) -> None:
    for s in d.steps:
        if isinstance(s, UpdateOrderStep) and any(_MONEY_FIELDS.search(k) for k in s.fields):
            r.errors.append(Finding(code="V_MONEY", message="automations cannot set prices or amounts", step=s.id))
        texts = [getattr(s, "text", None), getattr(s, "reason", None), getattr(s, "preview", None)]
        for t in texts:
            if t and _LITERAL_MONEY.search(cel._TEMPLATE.sub("", t)):
                r.errors.append(
                    Finding(
                        code="V_MONEY",
                        message="amounts must come from the order (money(...)), not be typed in",
                        step=s.id,
                    )
                )


def _elapsed_before(d: AutomationDefinition) -> dict[str, float]:
    """Worst-case seconds since the trigger when each step starts (`until` waits count as unbounded = 30 days)."""
    worst: dict[str, float] = {d.steps[0].id: 0.0}
    order = _topological(d)
    for sid in order:
        s = d.step(sid)
        here = worst.get(sid, 0.0)
        cost = 0.0
        if s.type == "wait":
            cost = iso_seconds(s.duration) if s.duration else float(MAX_DURATION_S)
        elif s.type in ("wait_for_event", "approval"):
            cost = iso_seconds(s.timeout)
        for nxt in d.successors(s):
            worst[nxt] = max(worst.get(nxt, 0.0), here + cost)
    return worst


def _topological(d: AutomationDefinition) -> list[str]:
    indeg = {s.id: 0 for s in d.steps}
    for s in d.steps:
        for n in d.successors(s):
            indeg[n] += 1
    queue = deque(i for i, v in indeg.items() if v == 0)
    out = []
    while queue:
        sid = queue.popleft()
        out.append(sid)
        for n in d.successors(d.step(sid)):
            indeg[n] -= 1
            if indeg[n] == 0:
                queue.append(n)
    return out


def _worst_case_s(d: AutomationDefinition) -> float:
    worst = _elapsed_before(d)
    total = 0.0
    for sid, start in worst.items():
        s = d.step(sid)
        cost = 0.0
        if s.type == "wait" and s.duration:
            cost = iso_seconds(s.duration)
        elif s.type in ("wait_for_event", "approval"):
            cost = iso_seconds(s.timeout)
        total = max(total, start + cost)
    return total


def _window_and_quiet(d: AutomationDefinition, r: ValidationReport) -> None:
    worst = _elapsed_before(d)
    customer_trigger = d.trigger.type != "schedule"
    for s in d.steps:
        if not isinstance(s, SendStep) or s.to != "customer":
            continue
        late = worst.get(s.id, 0.0) > DAY_S or not customer_trigger
        if late and s.template_fallback is None:
            channels = set(d.trigger.channels)
            if channels and channels <= {"facebook_messenger", "instagram_dm"}:
                r.errors.append(
                    Finding(code="V_WINDOW", message="Messenger/Instagram can't message after 24 h", step=s.id)
                )
            else:
                r.owner_questions.append(
                    Finding(
                        code="V_WINDOW",
                        message="This message may go out more than 24 h after the customer's last message; "
                        "WhatsApp then needs an approved template. Use the 'order_update' template?",
                        step=s.id,
                        options=["Yes, use order_update", "No, skip if the window is closed"],
                    )
                )
    for s in d.steps:
        if isinstance(s, WaitStep) and s.until and (m := _UNTIL_TIME.search(s.until)):
            hour = int(m.group(1))
            if hour < 9 or hour >= 21:
                r.warnings.append(
                    Finding(code="V_QUIET", message="lands in quiet hours (21:00–09:00); sent at 09:00", step=s.id)
                )


def compile_definition(d: AutomationDefinition) -> tuple[Executor, TriggerIndex]:
    executor: Executor = (
        "guard" if d.kind == "guard" else "durable" if any(s.type in DURABLE_STEPS for s in d.steps) else "instant"
    )
    keywords = sorted({k.strip().lower() for k in d.trigger.keywords_any if k.strip()})
    return executor, TriggerIndex(
        type=d.trigger.type,
        channels=d.trigger.channels,
        keywords=keywords,
        post_id=d.trigger.post_id,
        to_status=d.trigger.to_status,
        guard=d.kind == "guard",
    )


def until_hours_before(expr: str) -> tuple[str | None, int | None]:
    """For read-backs: ("10:00", 24) from `order.delivery_date.at('10:00') - duration('24h')`."""
    t = _UNTIL_TIME.search(expr)
    b = _UNTIL_BEFORE.search(expr)
    return (f"{int(t.group(1)):02d}:{t.group(2)}" if t else None, int(b.group(1)) if b else None)
