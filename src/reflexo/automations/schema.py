"""Automation definition language, `schema_version = 1` (POC_06 §1).

The AI compiles the owner's words into this typed definition once; the engine then runs it deterministically.
Templates are `{{ <CEL expr> }}` inside strings; filters and conditions are CEL (see `automations.cel`).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TriggerType = Literal[
    "message_received",
    "comment_received",
    "order_created",
    "order_confirmed",
    "order_cancelled",
    "order_status_changed",
    "payment_link_created",
    "payment_captured",
    "payment_failed",
    "order_confirming",
    "schedule",
    "manual",
]
_ISO_DURATION = re.compile(r"^P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Trigger(_Strict):
    type: TriggerType
    channels: list[str] = []
    keywords_any: list[str] = []
    first_message_only: bool = False
    post_id: str | None = None
    to_status: list[str] = []
    cron: str | None = None
    cel: str | None = None


class Limits(_Strict):
    max_runs_per_instance_per_day: int = Field(default=10, ge=1, le=100)
    max_steps: int = Field(default=25, ge=1, le=25)
    max_duration: str = "P30D"


class TemplateFallback(_Strict):
    name: str
    vars: list[str] = []


def _duration(v: str | None) -> str | None:
    if v is not None and (not _ISO_DURATION.match(v) or v in ("P", "PT")):
        raise ValueError(f"'{v}' is not an ISO 8601 duration like PT2H or P1D")
    return v


class _Step(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class SendStep(_Step):
    type: Literal["send"]
    to: Literal["customer", "owner"] = "customer"
    text: str | None = None
    blocks: list[dict[str, Any]] | None = None
    template_fallback: TemplateFallback | None = None
    next: str | None = None

    @model_validator(mode="after")
    def _content(self) -> SendStep:
        if bool(self.text) == bool(self.blocks):
            raise ValueError("send needs exactly one of text / blocks")
        return self


class WaitStep(_Step):
    type: Literal["wait"]
    duration: str | None = None
    until: str | None = None
    next: str | None = None

    @model_validator(mode="after")
    def _one(self) -> WaitStep:
        _duration(self.duration)
        if bool(self.duration) == bool(self.until):
            raise ValueError("wait needs exactly one of duration / until")
        return self


class WaitForEventStep(_Step):
    type: Literal["wait_for_event"]
    event: Literal["message_received", "payment_captured", "order_status_changed"]
    match: str = "true"
    consume: bool = True
    timeout: str  # required (V_WAIT_TIMEOUT)
    next: str | None = None
    on_timeout: str | None = None

    @model_validator(mode="after")
    def _t(self) -> WaitForEventStep:
        _duration(self.timeout)
        return self


class BranchStep(_Step):
    type: Literal["branch"]
    cel: str
    then: str
    else_: str = Field(alias="else")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CounterLimitStep(_Step):
    type: Literal["counter_limit"]
    key: str
    limit: int = Field(ge=1, le=100_000)
    amount: str = "1"
    on_exhausted: Literal["deny", "skip", "goto"] = "deny"
    goto: str | None = None
    release_on: list[Literal["order_cancelled", "payment_link_expired"]] = []
    next: str | None = None


class ClaimOnceStep(_Step):
    type: Literal["claim_once"]
    key: str
    on_claimed: Literal["skip", "goto"] = "skip"
    goto: str | None = None
    next: str | None = None


class DenyStep(_Step):
    type: Literal["deny"]
    reason: str
    alternatives: Literal["next_available_dates"] | None = None


class LlmClassifyStep(_Step):
    type: Literal["llm_classify"]
    input: str
    labels: list[str] = Field(min_length=2, max_length=10)
    output_var: str
    next: str | None = None


class LlmGenerateStep(_Step):
    type: Literal["llm_generate"]
    instruction: str
    input: str
    max_chars: int = Field(default=400, ge=20, le=1000)
    output_var: str
    next: str | None = None


class UpdateOrderStep(_Step):
    type: Literal["update_order"]
    fields: dict[str, Any]
    next: str | None = None


class UpdateCustomerStep(_Step):
    type: Literal["update_customer"]
    add_tags: list[str] = []
    remove_tags: list[str] = []
    next: str | None = None


class CreatePaymentLinkStep(_Step):
    type: Literal["create_payment_link"]
    for_: Literal["full", "advance", "balance"] = Field(default="full", alias="for")
    next: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ApprovalStep(_Step):
    type: Literal["approval"]
    preview: str
    timeout: str
    on_timeout: Literal["reject", "approve"] = "reject"
    next: str | None = None
    on_reject: str | None = None

    @model_validator(mode="after")
    def _t(self) -> ApprovalStep:
        _duration(self.timeout)
        return self


class NotifyOwnerStep(_Step):
    type: Literal["notify_owner"]
    text: str
    priority: Literal["p0", "p1", "p2", "p3"] = "p1"
    next: str | None = None


class EndStep(_Step):
    type: Literal["end"]


Step = Annotated[
    SendStep
    | WaitStep
    | WaitForEventStep
    | BranchStep
    | CounterLimitStep
    | ClaimOnceStep
    | DenyStep
    | LlmClassifyStep
    | LlmGenerateStep
    | UpdateOrderStep
    | UpdateCustomerStep
    | CreatePaymentLinkStep
    | ApprovalStep
    | NotifyOwnerStep
    | EndStep,
    Field(discriminator="type"),
]
DURABLE_STEPS = {"wait", "wait_for_event", "approval"}
GUARD_STEPS = {"counter_limit", "branch", "deny", "end"}


class AutomationDefinition(_Strict):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["flow", "guard"] = "flow"
    mode: Literal["consume", "observe"] = "observe"
    trigger: Trigger
    instance_key: str = "{{ event.customer_id }}"
    steps: list[Step] = Field(min_length=1)
    limits: Limits = Limits()

    def step(self, step_id: str) -> Any:
        return next(s for s in self.steps if s.id == step_id)

    def successors(self, s: Any) -> list[str]:
        """Step ids a step can continue to (for graph checks)."""
        out = []
        for attr in ("next", "then", "else_", "goto", "on_timeout", "on_reject"):
            v = getattr(s, attr, None)
            if isinstance(v, str):
                out.append(v)
        if getattr(s, "type", None) == "counter_limit" and s.on_exhausted == "deny":
            out += [d.id for d in self.steps if d.type == "deny"]
        return out


def iso_seconds(duration: str) -> float:
    """PT2H → 7200; P1D → 86400 (days, hours, minutes, seconds only)."""
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", duration)
    if not m:
        raise ValueError(duration)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(((d * 24 + h) * 60 + mi) * 60 + s)
