"""Messages written by purpose, then checked (BETA_13 §6; ADR-033).

Every message the system starts (owner notifications, proposals, questions, holding replies) is defined by its
purpose in `config/messages/purposes.yaml`, never by its words. The caller passes the facts, read from records,
and a short context; the `messages` role writes the message for the situation; a deterministic check requires
every listed fact, link and button and forbids any amount, link or phone number that is not a fact. One repair
round with the exact failure; after a second failure, or with no model, the fallback of §6.3: for an owner a fact
card (labelled facts and the buttons, no sentence), for a customer one holding line a day. A message is never
sent unchecked.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import text as sql

import reflexo.knowledge.api as knowledge
from reflexo.agent import names, output_gate
from reflexo.agent.prompts import prompt_version, render
from reflexo.agent.schemas import GateCheck, GateResult
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import ModelGatewayPort, OwnerNotifyPort
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, PriorityClass
from reflexo.common.llm import CallCtx, ChatMessage, ModelError
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.timeutil import utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)
Audience = Literal["customer", "owner", "staff", "prospect"]
ROLE = "messages"
PROMPT = "messages"
DEFAULT_LANGUAGE = "en"


class ButtonSpec(BaseModel):
    id: str  # fixed and handled deterministically: 'approve', 'paid'
    meaning: str  # what pressing it does, in plain words, for the writer
    max_label_chars: int = 20


class MessagePurpose(BaseModel):
    key: str
    audience: Audience
    purpose: str
    facts: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    buttons: list[ButtonSpec] = Field(default_factory=list)
    max_chars: int = 600
    public: bool = False


class Amount(BaseModel):
    """A money fact: checked by value, written in any usual form."""

    paise: int

    def __str__(self) -> str:
        return format_inr(self.paise)


class RecipientRef(BaseModel):
    kind: Audience
    conversation_id: UUID | None = None
    language: str | None = None


class ButtonLabel(BaseModel):
    id: str
    label: str


class Written(BaseModel):
    """What the `messages` role returns."""

    text: str
    buttons: list[ButtonLabel] = Field(default_factory=list)


class ComposedMessage(BaseModel):
    text: str
    buttons: list[ButtonLabel] = Field(default_factory=list)
    purpose_key: str
    check: GateResult
    fallback_used: bool
    language: str
    rejected: str | None = None  # the last draft that failed its check: kept for evals and X-ray, never sent
    fallback_reason: str | None = None  # check_failed | model:<code> | no_model


class UnknownPurpose(KeyError):
    pass


# ---------------------------------------------------------------- configuration


def _dir() -> Path:
    return Path(get_settings().messages_dir)


@lru_cache(maxsize=1)
def purposes() -> dict[str, MessagePurpose]:
    raw = yaml.safe_load((_dir() / "purposes.yaml").read_text(encoding="utf-8")) or []
    return {p["key"]: MessagePurpose.model_validate(p) for p in raw}


@lru_cache(maxsize=8)
def fallbacks(language: str) -> dict[str, Any]:
    """`config/messages/fallbacks/<language>.yaml`, English when the language has none."""
    path = _dir() / "fallbacks" / f"{language}.yaml"
    if not path.exists():
        path = _dir() / "fallbacks" / f"{DEFAULT_LANGUAGE}.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def forget() -> None:
    purposes.cache_clear()
    fallbacks.cache_clear()


def purpose(key: str) -> MessagePurpose:
    try:
        return purposes()[key]
    except KeyError as exc:
        raise UnknownPurpose(key) from exc


# ---------------------------------------------------------------- the check


def _present(facts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in facts.items() if v is not None and str(v).strip() != ""}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _allowed_amounts(facts: dict[str, Any]) -> set[int]:
    """Money facts, and every number written in a fact ("the order total (₹) is above 2000" may become ₹2,000)."""
    allowed: set[int] = set()
    for v in facts.values():
        if isinstance(v, Amount):
            allowed.add(v.paise)
        else:
            allowed |= output_gate.stated_amounts(str(v))
            allowed |= {round(float(n.replace(",", "")) * 100) for n in _NUMBER.findall(str(v))}
    return allowed


def check_message(
    p: MessagePurpose, written: Written, facts: dict[str, Any], links: dict[str, str] | None = None
) -> GateResult:
    """Deterministic, with the output gate's extractors (BETA_13 §6.1 step 3)."""
    started = time.perf_counter()
    facts = _present(facts)
    links = links or {}
    body = written.text
    checks: list[GateCheck] = []

    missing = []
    for key in p.facts:
        if key not in facts:
            continue
        value = facts[key]
        if isinstance(value, Amount):
            found = {a.paise for a in output_gate.extract_amounts(body)} | output_gate.stated_amounts(body)
            if value.paise not in found:
                missing.append(f"{key} = {value}")
        elif key.endswith("name"):  # a name: as written, or the same name in the recipient's script
            if not names.appears(str(value), body):
                missing.append(f"{key} = {value}")
        elif _norm(str(value)) not in _norm(body):
            missing.append(f"{key} = {value}")
    checks.append(GateCheck(
        name="facts", passed=not missing, detail="; ".join(missing) or None,
        guidance="Include each of these exactly as given." if missing else None,
    ))  # fmt: skip

    absent_links = [f"{k} = {v}" for k, v in links.items() if k in p.links and v not in body]
    checks.append(GateCheck(
        name="links", passed=not absent_links, detail="; ".join(absent_links) or None,
        guidance="Copy each link exactly." if absent_links else None,
    ))  # fmt: skip

    labels = {b.id: b.label.strip() for b in written.buttons}
    bad_buttons = [
        f"{b.id}: no label" if not labels.get(b.id)
        else f"{b.id}: '{labels[b.id]}' has {len(labels[b.id])} characters, at most {b.max_label_chars}"
        for b in p.buttons
        if not labels.get(b.id) or len(labels[b.id]) > b.max_label_chars
    ]  # fmt: skip
    checks.append(GateCheck(
        name="buttons", passed=not bad_buttons, detail="; ".join(bad_buttons) or None,
        guidance="Give every button id a short label within its limit." if bad_buttons else None,
    ))  # fmt: skip

    allowed = _allowed_amounts(facts)
    invented = [a.raw for a in output_gate.extract_amounts(body) if a.explicit and a.paise not in allowed]
    fact_text = " ".join(str(v) for v in facts.values()) + " " + " ".join(links.values())
    urls = [m.group(0) for m in output_gate._URL.finditer(body) if m.group(0).rstrip(".,") not in fact_text]
    known_phones = {output_gate._digits(m.group(0)) for m in output_gate._PHONE.finditer(fact_text)}
    phones = [
        m.group(0)
        for m in output_gate._PHONE.finditer(body)
        if not output_gate._phone_allowed(m.group(0), known_phones)
    ]
    percents = [m.group(0) for m in output_gate._PCT.finditer(body) if m.group(0) not in fact_text]
    extra = invented + urls + phones + percents
    checks.append(GateCheck(
        name="nothing_invented", passed=not extra, detail="; ".join(extra) or None,
        guidance="Remove every amount, link, phone number or discount that is not in the facts." if extra else None,
    ))  # fmt: skip

    too_long = len(body) > p.max_chars
    checks.append(GateCheck(
        name="length", passed=not too_long and bool(body.strip()),
        detail=f"{len(body)} characters" if too_long else (None if body.strip() else "empty"),
        guidance=f"Keep it under {p.max_chars} characters." if too_long else None,
    ))  # fmt: skip
    return GateResult(
        passed=all(c.passed for c in checks), checks=checks, duration_ms=(time.perf_counter() - started) * 1000
    )


# ---------------------------------------------------------------- writing


def _fact_lines(facts: dict[str, Any], links: dict[str, str]) -> str:
    lines = [f"- {k}: {v}" for k, v in facts.items()] + [f"- {k}: {v}" for k, v in links.items()]
    return "\n".join(lines) or "(none)"


def _exact_lines(p: MessagePurpose, facts: dict[str, Any], links: dict[str, str]) -> str:
    lines = []
    for key in p.facts:
        if key in facts:
            lines.append(f"- {facts[key]}   [{key}{', amount' if isinstance(facts[key], Amount) else ''}]")
    lines += [f"- {links[k]}   [{k}]" for k in p.links if k in links]
    return "\n".join(lines) or "(none)"


def _context_lines(context: dict[str, Any]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in context.items() if v not in (None, "")) or "(nothing more)"


def _button_lines(p: MessagePurpose) -> str:
    if not p.buttons:
        return "(none)"
    return "\n".join(f"- {b.id}: {b.meaning} [{b.max_label_chars}]" for b in p.buttons)


def language_for(recipient: RecipientRef, settings: dict[str, Any]) -> str:
    if get_settings().language_policy != "auto":
        return DEFAULT_LANGUAGE
    if recipient.language:
        return recipient.language
    if recipient.kind == "owner":
        return str(settings.get("owner_language") or DEFAULT_LANGUAGE)
    return str(settings.get("default_language") or DEFAULT_LANGUAGE)


def fact_card(p: MessagePurpose, facts: dict[str, Any], language: str) -> ComposedMessage:
    """`fallback.owner_fact_card`: no sentence at all; the facts as labelled lines, with the buttons."""
    words = fallbacks(language).get("owner_fact_card") or fallbacks(DEFAULT_LANGUAGE)["owner_fact_card"]
    present = _present(facts)
    lines = [str(words["titles"].get(p.key, p.key))]
    for key in p.facts + [k for k in present if k not in p.facts]:
        if key in present:
            lines.append(f"{words['facts'].get(key, key)}: {present[key]}")
    buttons = [
        ButtonLabel(id=b.id, label=str(words["buttons"].get(b.id, b.id))[: b.max_label_chars]) for b in p.buttons
    ]
    return ComposedMessage(
        text="\n".join(lines),
        buttons=buttons,
        purpose_key=p.key,
        check=GateResult(passed=True, checks=[GateCheck(name="fallback", passed=True)]),
        fallback_used=True,
        language=language,
    )


async def compose(
    purpose_key: str,
    *,
    business_id: UUID,
    recipient: RecipientRef,
    facts: dict[str, Any],
    context: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
    language: str | None = None,
) -> ComposedMessage:
    """Write, check, repair once; else the fallback. Never returns an unchecked message."""
    p = purpose(purpose_key)
    settings = await knowledge.business_settings(business_id)
    lang = language or language_for(recipient, settings)
    present = _present(facts)
    links = links or {}
    gateway = ports.registry.optional(ModelGatewayPort)  # type: ignore[type-abstract]
    prompt = render(
        PROMPT,
        audience=p.audience,
        purpose=p.purpose,
        facts=_fact_lines(present, links),
        exact=_exact_lines(p, present, links),
        context=_context_lines(context or {}),
        buttons=_button_lines(p),
        language=str(fallbacks(lang).get("language_name") or lang),
        max_chars=str(p.max_chars),
    )
    ctx = CallCtx(
        business_id=business_id,
        purpose=f"message:{p.key}",
        conversation_id=recipient.conversation_id,
        feature="messages",
        prompt_version=prompt_version(PROMPT),
    )
    conversation: list[ChatMessage] = [ChatMessage(role="user", content=prompt)]
    result: GateResult | None = None
    rejected: Written | None = None
    reason = "no_model"
    attempts = 0
    if gateway is not None:
        for attempts in range(1, int(get_settings().message_repair_rounds) + 2):
            try:
                written = await gateway.structured(ROLE, conversation, Written, ctx=ctx)
            except ModelError as exc:
                reason = f"model:{exc.code}"
                break
            written = _fit_labels(p, written, lang)
            result = check_message(p, written, present, links)
            if result.passed:
                composed = ComposedMessage(
                    text=written.text.strip(),
                    buttons=[b for b in written.buttons if b.id in {s.id for s in p.buttons}],
                    purpose_key=p.key,
                    check=result,
                    fallback_used=False,
                    language=lang,
                )
                await _trace(business_id, p, composed, attempts, None)
                return composed
            reason = "check_failed"
            rejected = written
            conversation += [
                ChatMessage(role="assistant", content=written.model_dump_json()),
                ChatMessage(role="user", content=_repair(result)),
            ]
    composed = fallback(p, present, lang).model_copy(update={"fallback_reason": reason})
    if result is not None and not result.passed:
        composed = composed.model_copy(
            update={"check": result, "rejected": rejected.model_dump_json() if rejected else None}
        )
    await _trace(business_id, p, composed, attempts, reason)
    return composed


def _fit_labels(p: MessagePurpose, written: Written, language: str) -> Written:
    """A label over its limit becomes the button's fixed label (`config/messages/fallbacks/`); a missing one stays
    missing, so the check still fails it."""
    words = (fallbacks(language).get("owner_fact_card") or {}).get("buttons") or {}
    limits = {b.id: b.max_label_chars for b in p.buttons}
    fitted = [
        ButtonLabel(id=b.id, label=str(words.get(b.id, b.label))[: limits[b.id]])
        if b.id in limits and len(b.label.strip()) > limits[b.id]
        else b
        for b in written.buttons
    ]
    return written.model_copy(update={"buttons": fitted})


def _repair(result: GateResult) -> str:
    problems = [f"- {c.name}: {c.detail or ''}" + (f" -> {c.guidance}" if c.guidance else "") for c in result.failures]
    return render("messages_repair", problems="\n".join(problems))


def fallback(p: MessagePurpose, facts: dict[str, Any], language: str) -> ComposedMessage:
    if p.audience == "customer":
        line = str(fallbacks(language).get("customer_holding") or fallbacks(DEFAULT_LANGUAGE)["customer_holding"])
        return ComposedMessage(
            text=line, purpose_key=p.key, fallback_used=True, language=language,
            check=GateResult(passed=True, checks=[GateCheck(name="fallback", passed=True)]),
        )  # fmt: skip
    return fact_card(p, facts, language)


async def _trace(business_id: UUID, p: MessagePurpose, m: ComposedMessage, attempts: int, reason: str | None) -> None:
    await emit(
        "compose",
        PipelineEventStatus.ERROR if m.fallback_used else PipelineEventStatus.OK,
        business_id=business_id,
        purpose=p.key,
        fallback_used=m.fallback_used,
        attempts=attempts,
        reason=reason,
        failures=[c.name for c in m.check.failures],
    )


# ---------------------------------------------------------------- sending to the owner


async def notify_owner(
    purpose_key: str,
    *,
    business_id: UUID,
    facts: dict[str, Any],
    context: dict[str, Any] | None = None,
    kind: str,
    dedupe_key: str,
    priority: PriorityClass = PriorityClass.P1,
    buttons: dict[str, str] | None = None,
    fixed_buttons: list[tuple[str, str]] | None = None,
    ref: dict[str, str] | None = None,
    about_conversation: UUID | None = None,
    media: list[UUID] | None = None,
) -> ComposedMessage | None:
    """Compose for the owner and hand it to the owner's channels. `buttons` maps each purpose button id to its
    callback; `fixed_buttons` are (callback, label) pairs whose labels come from records (an answer option)."""
    notifier = ports.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
    if notifier is None:
        return None
    composed = await compose(
        purpose_key,
        business_id=business_id,
        recipient=RecipientRef(kind="owner", conversation_id=about_conversation),
        facts=facts,
        context=context,
    )
    callbacks = buttons or {}
    rendered = list(fixed_buttons or []) + [(callbacks[b.id], b.label) for b in composed.buttons if b.id in callbacks]
    await notifier.notify(
        business_id,
        kind,
        composed.text,
        dedupe_key=dedupe_key,
        priority=priority,
        buttons=rendered or None,
        ref=ref,
        about_conversation=about_conversation,
        media=media,
    )
    return composed


# ---------------------------------------------------------------- the customer holding fallback


async def holding_line(
    business_id: UUID, conversation_id: UUID, language: str, *, still_checking: bool = False
) -> tuple[str, str] | None:
    """`fallback.customer_holding` (BETA_13 §6.3): (text, idempotency base), or None when this conversation already
    had its holding lines today (REFLEXO_FALLBACK_HOLDING_PER_DAY). The base is unique per conversation, day and
    count, so a retried send never adds a line."""
    settings = await knowledge.business_settings(business_id)
    day = utcnow().astimezone(ZoneInfo(str(settings.get("timezone") or "Asia/Kolkata"))).strftime("%Y%m%d")
    prefix = f"holdfb:{conversation_id}:{day}:"
    async with get_db().tenant_session(business_id) as s:
        sent = (
            await s.execute(
                sql("""SELECT count(DISTINCT split_part(idempotency_key, ':', 4)) FROM outbound_messages
                       WHERE conversation_id = :c AND idempotency_key LIKE :p"""),
                {"c": conversation_id, "p": prefix + "%"},
            )
        ).scalar_one()
    if int(sent) >= int(get_settings().fallback_holding_per_day):
        return None
    words = fallbacks(language)
    key = "customer_still_checking" if still_checking else "customer_holding"
    line = str(words.get(key) or fallbacks(DEFAULT_LANGUAGE)[key])
    own = settings.get("holding_messages") or {}  # the shop's own holding line, when it set one
    if not still_checking and (own.get(language) or own.get(DEFAULT_LANGUAGE)):
        line = str(own.get(language) or own.get(DEFAULT_LANGUAGE))
    return line, f"{prefix}{int(sent)}"
