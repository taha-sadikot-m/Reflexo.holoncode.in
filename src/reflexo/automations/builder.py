"""Builder (POC_06 §4.1 step 1): the owner's words → up to 3 automation definitions (builder role, JSON).

The model only drafts; the validator decides. Validation errors are fed back for at most 2 repair rounds by the
workflow. One owner question is resolved deterministically: a customer message that can land after the 24 h
window gets the approved `order_update` template as fallback (the read-back says so).
"""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

import reflexo.knowledge.api as knowledge
from reflexo.automations import demo
from reflexo.automations.validator import Finding, parse, validate
from reflexo.common import ports
from reflexo.common.contracts import ModelGatewayPort
from reflexo.common.llm import CallCtx, ChatMessage

MAX_AUTOMATIONS = 3
APPROVED_TEMPLATES = ["order_update"]

CATALOGUE = """\
DEFINITION: {schema_version: 1, name, kind: "flow"|"guard", mode: "observe"|"consume", trigger, instance_key, steps[]}
TRIGGERS (trigger.type): message_received {channels[], keywords_any[], cel} · comment_received {keywords_any[]} ·
  order_created | order_confirmed | order_cancelled {cel} · order_status_changed {to_status[]} ·
  payment_link_created | payment_captured | payment_failed · order_confirming (guards only) · schedule {cron} · manual
STEPS (each has a unique lowercase id and `next` unless it ends):
  send {to: customer|owner, text | blocks, template_fallback?: {name, vars[]}}
  wait {duration: ISO 8601 like PT2H/P1D | until: CEL timestamp}
  wait_for_event {event: message_received|payment_captured|order_status_changed, match: CEL, consume, timeout,
    on_timeout}
  branch {cel, then, else} · counter_limit {key, limit, amount, on_exhausted: deny|skip|goto, release_on[]}
  claim_once {key, on_claimed} · deny {reason, alternatives?: "next_available_dates"} (guards)
  notify_owner {text, priority: p0..p3} · approval {preview, timeout, on_timeout: reject} · end
TEMPLATES: "{{ <CEL> }}" inside strings. Variables: event, customer{name}, order{id, number, items, total_paise,
  delivery_date, delivery_slot, payment_method, payment_status, status}, business{name}, now.
CEL FUNCTIONS: contains_any(text, [..]), lower(s), money(paise), date_fmt(ts, "d MMM"), hours_since(ts),
  order.delivery_date.at("HH:MM") (timestamp), order.has_category("cakes"), order.cake_count(), duration("24h").
RULES: never put prices/amounts/discounts in texts (use money(order.total_paise)); a guard is kind "guard" with trigger
  order_confirming and only counter_limit/branch/deny/end steps; every wait_for_event/approval needs a timeout;
  customer messages that may go out >24 h after the trigger need template_fallback {name: "order_update",
  vars: [<the message>]}; no loops; write customer texts in the owner's language (Hinglish if they wrote Hinglish).
"""


class BuilderDraft(BaseModel):
    automations: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_AUTOMATIONS)


async def draft(
    business_id: UUID,
    description: str,
    *,
    feedback: list[str] | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cards = await knowledge.search_catalog(business_id, "cake", k=10)
    categories = sorted({c.category.lower() for c in cards if c.category}) or ["cakes"]
    schemas = await knowledge.detail_schemas(business_id)
    fields = sorted({f["key"] for s in schemas for f in s.get("fields", [])})
    examples = "\n".join(json.dumps(x, ensure_ascii=False) for x in demo.ALL)
    parts = [
        "You compile a shop owner's instruction into automation definitions (JSON). Return "
        f'{{"automations": [...]}} with 1–{MAX_AUTOMATIONS} definitions — one per distinct rule in the instruction.',
        CATALOGUE,
        f"BUSINESS: categories {categories}; order detail fields {fields}; channels whatsapp, telegram; "
        f"approved WhatsApp templates {APPROVED_TEMPLATES}.",
        "EXAMPLES (one definition per line):\n" + examples,
        f"OWNER'S INSTRUCTION: {description}",
    ]
    if feedback and previous:
        parts.append(
            "YOUR PREVIOUS DRAFT:\n"
            + json.dumps(previous, ensure_ascii=False)
            + "\nIT FAILED VALIDATION — fix exactly these problems and return the full corrected list:\n- "
            + "\n- ".join(feedback)
        )
    model = ports.registry.require(ModelGatewayPort)  # type: ignore[type-abstract]
    result = await model.structured(
        "builder",
        [ChatMessage(role="user", content="\n\n".join(parts))],
        BuilderDraft,
        ctx=CallCtx(business_id=business_id, purpose="automation.build"),
    )
    return result.automations


def check(definitions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate each draft; resolve the 24 h-window question with the approved template. Returns (definitions,
    problems) — problems are phrased for the repair round ("#2 COD confirmation: V_TYPES …")."""
    fixed: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, raw in enumerate(definitions, 1):
        raw = _sanitize(copy.deepcopy(raw))
        d, report = parse(raw)
        if d is not None:
            report = validate(d)
            if report.owner_questions and all(q.code == "V_WINDOW" for q in report.owner_questions):
                _add_template_fallback(raw, report.owner_questions)
                d, report = parse(raw)
                report = validate(d) if d is not None else report
        label = f"#{i} {raw.get('name', 'automation')}"
        problems += [f"{label}: {f.code} {f'(step {f.step}) ' if f.step else ''}{f.message}" for f in _blocking(report)]
        fixed.append(raw)
    return fixed, problems


def _sanitize(raw: dict[str, Any]) -> dict[str, Any]:
    """Template fallbacks are decided deterministically (only approved templates), never trusted from the model:
    live, Gemini wrote null/strings there and repair rounds could not fix it."""
    for step in raw.get("steps", []) if isinstance(raw.get("steps"), list) else []:
        if not isinstance(step, dict) or "template_fallback" not in step:
            continue
        tf = step["template_fallback"]
        if not (isinstance(tf, dict) and tf.get("name") in APPROVED_TEMPLATES and isinstance(tf.get("vars"), list)):
            del step["template_fallback"]
    return raw


def _blocking(report: Any) -> list[Finding]:
    return [*report.errors, *report.owner_questions]


def _add_template_fallback(raw: dict[str, Any], questions: list[Finding]) -> None:
    late = {q.step for q in questions}
    for step in raw.get("steps", []):
        if step.get("id") in late and step.get("type") == "send" and step.get("text"):
            step["template_fallback"] = {"name": APPROVED_TEMPLATES[0], "vars": [step["text"]]}
