"""The bridge to `reflexo-policy-check` (BETA_04 §7.6): JSON in on stdin, JSON out on stdout, 10 s timeout.

The binary is built from `tools/policy-check` (Rust, `cedar-policy-symcc`, the cvc5 solver). Production images
carry it; on a workstation `REFLEXO_POLICY_CHECK_CMD` runs the same Linux binary through `docker run`. It is
never called during a turn: only when the owner adds or edits a rule, and by the hourly re-check.

Comparisons are made on views of one layer that Cedar's two-valued answer can express:
- `acts`: Allow exactly when the layer lets the agent act without asking (allow rules permit; propose and deny
  rules forbid; the platform's forbids apply);
- `declines`: Allow exactly when a deny rule matches.
"""

from __future__ import annotations

import asyncio
import json
import math
import shlex
import subprocess
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from reflexo.common.config import get_settings
from reflexo.common.flags import flag
from reflexo.common.logging import get_logger
from reflexo.policy import reference
from reflexo.policy.compile import compile_rules
from reflexo.policy.models import EntityRef, PolicyRequest
from reflexo.policy.rules import FIELDS, Rule
from reflexo.policy.schema import PROBE_CONNECTOR_ACTION, full_schema, layer_text

log = get_logger(__name__)
Command = Literal["compare", "lint", "never-errors", "implies"]
HIDDEN_FLAG = "policy_checker_hidden"  # chaos `policy_checker_missing` (BETA_13 §7.6): behave as if not installed
View = Literal["acts", "declines"]


class Counterexample(BaseModel):
    principal: str
    action: str
    resource: str
    context: dict[str, Any] = Field(default_factory=dict)
    entities: list[dict[str, Any]] = Field(default_factory=list)


class Comparison(BaseModel):
    name: str
    relation: Literal["equivalent", "more_permissive", "less_permissive", "incomparable"]
    more: list[Counterexample] = Field(default_factory=list)  # allowed by new, not by old
    less: list[Counterexample] = Field(default_factory=list)  # allowed by old, not by new


class LintFinding(BaseModel):
    policy_id: str
    kind: Literal["impossible", "shadowed", "overrides_all"]
    by: str | None = None  # the policy that shadows it


class CheckReport(BaseModel):
    command: str
    available: bool  # False: the checker could not run (missing, timed out, crashed)
    error: str | None = None
    comparisons: list[Comparison] = Field(default_factory=list)  # compare
    findings: list[LintFinding] = Field(default_factory=list)  # lint
    may_error: list[str] = Field(default_factory=list)  # never-errors: policy ids that can error
    holds: bool | None = None  # implies
    counterexamples: list[Counterexample] = Field(default_factory=list)  # implies

    @property
    def clean(self) -> bool:
        return (
            self.available
            and self.error is None
            and not self.findings
            and not self.may_error
            and self.holds is not False
        )


def view(rules: Sequence[Rule], kind: View, *, layer: Literal["customer", "shop"] = "shop") -> str:
    """One layer as a two-valued Cedar set (module docstring)."""
    if kind == "acts":
        mapped = [r.model_copy(update={"effect": "allow" if r.effect == "allow" else "deny"}) for r in rules]
        return layer_text("platform") + "\n" + compile_rules(mapped, layer=layer).cedar_text
    mapped = [r.model_copy(update={"effect": "allow"}) for r in rules if r.effect == "deny"]
    return compile_rules(mapped, layer=layer).cedar_text


def checker_schema(fragment: str = "") -> str:
    return full_schema(fragment or PROBE_CONNECTOR_ACTION)


def _call(argv: list[str], body: bytes, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, input=body, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603


async def run(command: Command, payload: dict[str, Any]) -> CheckReport:
    """Call the binary; never raises. An unavailable checker is a report with `available = False`. Runs in a
    worker thread, so it works under any event loop."""
    if await flag(HIDDEN_FLAG, False):
        log.warning("policy_check_unavailable", command=command, error="hidden by chaos")
        return CheckReport(command=command, available=False, error="hidden by chaos")
    s = get_settings()
    argv = [*shlex.split(s.policy_check_cmd, posix=True), command]
    body = json.dumps({"command": command, **payload}).encode()
    try:
        proc = await asyncio.to_thread(_call, argv, body, s.policy_check_timeout_s)
    except subprocess.TimeoutExpired:
        log.warning("policy_check_timeout", command=command)
        return CheckReport(command=command, available=False, error="timeout")
    except OSError as exc:
        log.warning("policy_check_unavailable", command=command, error=type(exc).__name__)
        return CheckReport(command=command, available=False, error=f"not runnable: {type(exc).__name__}")
    if proc.returncode != 0:
        log.warning("policy_check_failed", command=command, code=proc.returncode, stderr=proc.stderr.decode()[-300:])
        return CheckReport(command=command, available=False, error=f"exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout.decode())
    except ValueError:
        return CheckReport(command=command, available=False, error="unreadable output")
    return CheckReport.model_validate({"command": command, "available": True, **data})


async def check(command: Command, **args: Any) -> CheckReport:
    """`compare(pairs=[{name, old, new}])`, `lint(set=...)`, `never-errors(set=...)`, `implies(set, property)`;
    `fragment` adds a business's connector actions to the schema."""
    fragment = str(args.pop("fragment", "") or "")
    return await run(command, {"schema": checker_schema(fragment), **args})


# ---------------------------------------------------------------- counterexamples, as facts a person can read

ROUND_NUMBERS = (0, 1, 2, 5, 10, 20, 24, 48, 50, 72, 100, 200, 500, 1000, 2000, 5000, 10_000, 50_000, 100_000)


def as_request(cex: Counterexample) -> PolicyRequest:
    def ref(uid: str) -> EntityRef:
        head, _, quoted = uid.rpartition("::")
        return EntityRef(type=head.split("::")[-1], id=json.loads(quoted) if quoted.startswith('"') else quoted)

    action = ref(cex.action).id
    return PolicyRequest(
        principal=ref(cex.principal),
        action=action,
        resource=ref(cex.resource),
        context=dict(cex.context),
        entities=[e for e in cex.entities if not str(e["uid"]["type"]).endswith("::Action")],
        owner_approved=bool(cex.context.get("owner_approved")),
    )


def holds(req: PolicyRequest, rules: Sequence[Rule], kind: View, layer: str) -> bool:
    """What the `acts` / `declines` view answers for one request (the reference semantics of `view`)."""
    scope = "customer" if layer == "customer" else "global"
    found = reference._rules_layer(req, rules, scope, reference.situation(req))
    if kind == "declines":
        return found is not None and found[0] == "decline"
    return not reference._platform(req) and found is not None and found[0] == "act"


def _set_field(req: PolicyRequest, name: str, value: Any) -> PolicyRequest | None:
    spec = FIELDS[name]
    if spec.on == "context":
        return req.model_copy(update={"context": {**req.context, spec.attr: value}})
    target = reference.order_of(req) if spec.on == "order" else None
    uid = None
    if spec.on == "order":
        uid = req.resource.uid if req.resource.type == "Order" else None
    else:
        uid = reference.customer_uid(req)
    if uid is None or (spec.on == "order" and target is None):
        return None
    entities = [
        {**e, "attrs": {**e.get("attrs", {}), spec.attr: value}} if e["uid"] == uid else e for e in req.entities
    ]
    return req.model_copy(update={"entities": entities})


def example_facts(
    cex: Counterexample, old: Sequence[Rule], new: Sequence[Rule], kind: View, layer: str, direction: str
) -> dict[str, Any]:
    """The facts of one counterexample that the rules talk about, with each number replaced by the roundest
    value that keeps it a counterexample (checked on the reference evaluator)."""
    req = as_request(cex)
    first, second = (new, old) if direction == "more" else (old, new)

    def still(r: PolicyRequest) -> bool:
        return holds(r, first, kind, layer) and not holds(r, second, kind, layer)

    mentioned = [c for r in [*old, *new] if r.capability == req.action for c in r.conditions]
    fields = list(dict.fromkeys(c.field for c in mentioned))
    if still(req):
        for name in fields:
            spec = FIELDS[name]
            candidates: list[Any]
            if spec.kind == "number":
                thresholds = {float(c.value) for c in mentioned if c.field == name}  # type: ignore[arg-type]
                near = {v for t in thresholds for v in (math.floor(t), math.ceil(t), math.floor(t) + 1,
                                                       math.ceil(t) - 1)}  # fmt: skip
                candidates = sorted({*ROUND_NUMBERS, *near}, key=lambda v: (abs(v), v))
            elif spec.kind == "enum":
                candidates = list(spec.values)
            else:
                continue
            for value in candidates:
                tried = _set_field(req, name, int(value) if spec.kind == "number" else value)
                if tried is not None and still(tried):
                    req = tried
                    break
    sit = reference.situation(req)
    return {name: sit.get(name) for name in fields if name in sit}
