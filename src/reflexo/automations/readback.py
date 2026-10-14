"""Deterministic read-back (POC_06 §4.2): every trigger, filter, wait, limit, message and timing is stated, in the
owner's language (hi-Latn / en). No LLM — the owner confirms exactly what will run.
"""

from __future__ import annotations

import re
from typing import Any

from reflexo.automations.cel import expressions_in
from reflexo.automations.schema import AutomationDefinition, iso_seconds
from reflexo.automations.validator import until_hours_before

Lang = str  # "hi-Latn" | "en"

TRIGGERS: dict[str, tuple[str, str]] = {
    "order_confirmed": ("{cond}order confirm hote hi", "when {cond}an order is confirmed"),
    "order_created": ("naya {cond}order aate hi", "when a new {cond}order comes in"),
    "order_cancelled": ("{cond}order cancel hone par", "when {cond}an order is cancelled"),
    "order_status_changed": ("order '{to}' hone par", "when an order becomes '{to}'"),
    "payment_link_created": ("payment link bhejne ke baad", "after a payment link is sent"),
    "payment_captured": ("payment aate hi", "when a payment arrives"),
    "payment_failed": ("payment fail hone par", "when a payment fails"),
    "message_received": ("customer ka message aane par{kw}", "when a customer messages{kw}"),
    "comment_received": ("post par comment aane par{kw}", "when someone comments{kw}"),
    "order_confirming": ("har {cond}order confirm karte waqt", "whenever {cond}an order is being confirmed"),
    "schedule": ("schedule par ({cron})", "on schedule ({cron})"),
    "manual": ("jab aap kahein", "when you run it"),
}
LABELS: list[tuple[str, str, str]] = [  # (pattern in the expression, hi-Latn label, en label)
    (r"order\.number", "MB-xxxx", "MB-xxxx"),
    (r"money\(order\.total_paise\)", "total", "total"),
    (r"customer\.name", "customer ka naam", "customer's name"),
    (r"order\.delivery_slot", "slot", "slot"),
    (r"order\.delivery_date", "delivery date", "delivery date"),
    (r"order\.items", "items", "items"),
]


def _label(expr: str, lang: Lang) -> str:
    for pattern, hi, en in LABELS:
        if re.search(pattern, expr):
            return hi if lang == "hi-Latn" else en
    return expr


def _quote(text_: str, lang: Lang) -> str:
    out = text_
    for expr in expressions_in(text_):
        out = re.sub(r"\{\{\s*" + re.escape(expr) + r"\s*\}\}", "{" + _label(expr, lang) + "}", out)
    return f"“{out}”"


def _duration(d: str, lang: Lang) -> str:
    seconds = iso_seconds(d)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    hi = {"d": "din", "h": "ghante", "m": "minute"}
    en = {"d": "day", "h": "hour", "m": "minute"}
    parts = []
    for n, k in ((days, "d"), (hours, "h"), (minutes, "m")):
        if n:
            parts.append(f"{n} {hi[k]}" if lang == "hi-Latn" else f"{n} {en[k]}{'s' if n > 1 else ''}")
    return " ".join(parts) or ("turant" if lang == "hi-Latn" else "immediately")


def _condition(cel: str | None, lang: Lang) -> str:
    if not cel:
        return ""
    if re.search(r"payment_method\s*==\s*['\"]cod['\"]", cel):
        return "COD "
    if m := re.search(r"has_category\(\s*['\"](\w+)['\"]\s*\)", cel):
        return f"{m.group(1)} "
    return f"(condition: {cel}) "


def _trigger(d: AutomationDefinition, lang: Lang) -> str:
    hi, en = TRIGGERS[d.trigger.type]
    kw = ""
    if d.trigger.keywords_any:
        words = ", ".join(d.trigger.keywords_any)
        kw = f" jisme ye words hon: {words}" if lang == "hi-Latn" else f" containing any of: {words}"
    return (hi if lang == "hi-Latn" else en).format(
        cond=_condition(d.trigger.cel, lang),
        to=", ".join(d.trigger.to_status),
        kw=kw,
        cron=d.trigger.cron or "",
    )


def _step_text(d: AutomationDefinition, s: Any, lang: Lang, seen: set[str]) -> list[str]:
    """Sentences for a step and the main path after it (branches described inline)."""
    if s.id in seen:
        return []
    seen.add(s.id)
    hi = lang == "hi-Latn"
    out: list[str] = []
    nxt: str | None = getattr(s, "next", None)
    if s.type == "send":
        content = _quote(s.text, lang) if s.text else ("ek product card" if hi else "a product card")
        who = ("customer" if s.to == "customer" else "aap") if hi else ("the customer" if s.to == "customer" else "you")
        out.append(f"{who} ko message bhejungi: {content}" if hi else f"send {who}: {content}")
        if s.template_fallback:
            out.append(
                f"(24 ghante ki window band ho to template '{s.template_fallback.name}' se)"
                if hi
                else f"(using the '{s.template_fallback.name}' template if the 24 h window is closed)"
            )
    elif s.type == "wait":
        if s.duration:
            out.append(f"{_duration(s.duration, lang)} baad" if hi else f"after {_duration(s.duration, lang)}")
        else:
            at, before = until_hours_before(s.until)
            if at and before == 24:
                out.append(f"delivery se ek din pehle {at} baje" if hi else f"one day before delivery at {at}")
            elif at and before:
                out.append(
                    f"delivery se {before} ghante pehle ({at} ke hisaab se)"
                    if hi
                    else f"{before} hours before delivery (from {at})"
                )
            else:
                out.append(f"is samay: {s.until}" if hi else f"at: {s.until}")
    elif s.type == "wait_for_event":
        what = {
            "message_received": ("customer ke reply", "the customer's reply"),
            "payment_captured": ("payment", "the payment"),
            "order_status_changed": ("order status update", "an order status update"),
        }[s.event]
        words = re.findall(r"'([^']+)'", s.match) if "contains_any" in s.match else []
        cond = (
            (f" (reply mein: {', '.join(words)})" if hi else f" (reply contains: {', '.join(words)})") if words else ""
        )
        out.append(
            f"{_duration(s.timeout, lang)} tak {what[0]} ka intezaar karungi{cond}"
            if hi
            else f"wait up to {_duration(s.timeout, lang)} for {what[1]}{cond}"
        )
        if s.consume and s.event == "message_received":
            out.append("(us reply par AI alag se jawab nahi degi)" if hi else "(the AI won't also answer that reply)")
        yes = _chain(d, s.next, lang, seen)
        no = _chain(d, s.on_timeout, lang, seen)
        if yes:
            out.append(("aaya to: " if hi else "if it comes: ") + yes)
        if no:
            out.append(("na aaye to: " if hi else "if not: ") + no)
        return out
    elif s.type == "counter_limit":
        thing = s.key.split(":")[0].rstrip("s") if ":" in s.key else "order"
        dims = [_label(e, lang) for e in expressions_in(s.key)]
        dim = dims[0] if dims else ("din" if hi else "day")
        out.append(
            f"har {dim} par maximum *{s.limit} {thing}* orders"
            if hi
            else f"at most *{s.limit} {thing}* orders per {dim}"
        )
        if s.on_exhausted == "deny":
            deny = next((x for x in d.steps if x.type == "deny"), None)
            alt = " aur agle available dates bataungi" if hi else " and offer the next available dates"
            alt = alt if deny is not None and deny.alternatives else ""
            out.append(
                f"{s.limit + 1}th order aane par customer ko politely mana karungi{alt}"
                if hi
                else f"the {s.limit + 1}th order is politely declined{alt}"
            )
            if deny is not None:
                seen.add(deny.id)
        if s.release_on:
            out.append(
                "(cancel ya payment expire hone par jagah wapas)" if hi else "(cancelled/expired orders free a slot)"
            )
    elif s.type == "claim_once":
        out.append("har customer ko sirf ek baar" if hi else "only once per customer")
    elif s.type == "branch":
        yes, no = _chain(d, s.then, lang, seen), _chain(d, s.else_, lang, seen)
        out.append((f"agar ({s.cel}): {yes}; warna: {no}") if hi else f"if ({s.cel}): {yes}; otherwise: {no}")
        return out
    elif s.type == "notify_owner":
        out.append(f"aapko bataungi: {_quote(s.text, lang)}" if hi else f"tell you: {_quote(s.text, lang)}")
    elif s.type == "approval":
        out.append(
            f"pehle aapse poochungi: {_quote(s.preview, lang)}" if hi else f"ask you first: {_quote(s.preview, lang)}"
        )
        rejected = _chain(d, s.on_reject, lang, seen)
        if rejected:
            out.append(("mana karein to: " if hi else "if you say no: ") + rejected)
    elif s.type == "update_order":
        fields = ", ".join(f"{k} = {v}" for k, v in s.fields.items())
        out.append(f"order update: {fields}" if hi else f"update the order: {fields}")
    elif s.type == "update_customer":
        out.append(f"customer tags: +{s.add_tags} -{s.remove_tags}")
    elif s.type == "create_payment_link":
        out.append("payment link bhejungi" if hi else "send a payment link")
    elif s.type in ("llm_classify", "llm_generate"):
        out.append("AI se message samjhungi/likhwaungi" if hi else "use AI to classify/write the message")
    if nxt:
        out += _step_text(d, d.step(nxt), lang, seen)
    return out


def _chain(d: AutomationDefinition, start: str | None, lang: Lang, seen: set[str]) -> str:
    if not start:
        return ""
    return ", ".join(_step_text(d, d.step(start), lang, seen)) or ("bas" if lang == "hi-Latn" else "done")


def describe(d: AutomationDefinition, lang: Lang = "hi-Latn") -> str:
    """One automation as one sentence block: '*Name:* trigger — steps.'"""
    trigger = _trigger(d, lang)
    body = ", ".join(_step_text(d, d.steps[0], lang, set()))
    return f"*{d.name}:* {trigger[0].upper()}{trigger[1:]}, {body}."


def readback(defs: list[AutomationDefinition], lang: Lang = "hi-Latn", demo_time_scale: float = 1.0) -> str:
    hi = lang == "hi-Latn"
    n = len(defs)
    head = (
        f"Samajh gayi, ye {n} rule{'s' if n > 1 else ''} lagaungi:"
        if hi
        else f"Got it, I'll set up {n} rule{'s' if n > 1 else ''}:"
    )
    lines = [head, *[f"{i}) {describe(d, lang)}" for i, d in enumerate(defs, 1)]]
    if demo_time_scale < 1:
        seconds = round(86400 * demo_time_scale)
        lines.append(f"⏱ Demo mode: 1 din = {seconds} sec." if hi else f"⏱ Demo mode: 1 day = {seconds} s.")
    return "\n".join(lines)
