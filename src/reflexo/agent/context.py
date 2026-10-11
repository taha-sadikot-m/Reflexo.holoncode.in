"""`prepare` (POC_04 §4.2–4.3): load everything the customer desk needs in parallel and lay out the prompt.

Static first (rules → business → channel → Q&A) so provider prefix caching works across turns of a business;
then the dynamic part (customer → speculative cards → conversation → now).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent import language as lang
from reflexo.agent import prompts
from reflexo.agent.tools.customer import card_json
from reflexo.common.contracts import TurnRef
from reflexo.common.db import get_db
from reflexo.common.timeutil import to_business_tz, utcnow

HISTORY = 20


@dataclass
class PreparedContext:
    system: str
    dynamic: str
    conversation: gateway.ConversationView
    inbound: list[gateway.MessageView]
    inbound_text: str
    language_guess: str
    settings: dict[str, Any]
    profile_version: int
    speculative_cards: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    open_order: dict[str, Any] | None = None  # compact tool view of the customer's draft/awaiting-payment order
    open_question: dict[str, Any] | None = None  # owner question still unanswered for this conversation
    recent_lines: list[str] = field(default_factory=list)  # last messages, formatted (owner notifications)


def _fmt_message(m: gateway.MessageView, tz: str) -> str:
    who = {"customer": "customer", "ai": "you", "system": "you", "owner": "team", "team": "team"}.get(
        m.author, m.author
    )
    when = to_business_tz(m.created_at, tz).strftime("%d %b %H:%M")
    body = m.text
    if m.interactive_option:
        body += f" [tapped option {m.interactive_option}]"
    for a in m.attachments:
        body += f" [{a.get('kind')}: {a.get('description') or 'not described yet'}]"
    return f"{when} {who}: {body}"


def _cards_block(cards: list[dict[str, Any]]) -> str:
    lines = []
    for c in cards:
        variants = " · ".join(f"{v['name']} {v['price']} (variant:{v['variant_id']})" for v in c["variants"])
        extra = f" · eggless {c['eggless']}" if c.get("eggless") else ""
        status = "" if c["in_stock"] else " [OUT OF STOCK]"
        price = variants or "PRICE ON REQUEST"
        lines.append(f"[offering:{c['offering_id']}] {c['name']} — {price}{extra}{status}")
    return "\n".join(lines) or "(none matched — use catalog_search if products are asked about)"


async def prepare(ref: TurnRef) -> PreparedContext:
    started = time.perf_counter()
    async with get_db().tenant_session(ref.business_id) as s:
        conv = await gateway.conversation_view(s, ref.conversation_id)
        if conv is None:
            raise LookupError(f"conversation {ref.conversation_id} not found")
        message_ids = list(dict.fromkeys(ref.message_ids))
        inbound = await gateway.messages_by_ids(s, message_ids)
        history = await gateway.recent_messages(s, ref.conversation_id, limit=HISTORY, exclude=message_ids)
        pending = (
            await s.execute(
                text("""SELECT short_code, question, asked_at FROM owner_questions
                        WHERE conversation_id = :c AND status = 'open' ORDER BY asked_at DESC LIMIT 1"""),
                {"c": ref.conversation_id},
            )
        ).first()
    inbound_text = "\n".join(m.text for m in inbound if m.text).strip()
    customer_id = conv.customer.id if conv.customer else None

    # speculative retrieval starts first so its two queries get pool connections before the other loads
    spec_task = asyncio.create_task(knowledge.speculative(ref.business_id, inbound_text, customer_id=customer_id))
    await asyncio.sleep(0)
    profile, settings, qas, biz_profile, order = await asyncio.gather(
        knowledge.profile_block(ref.business_id),
        knowledge.business_settings(ref.business_id),
        knowledge.qa_block(ref.business_id),
        knowledge.get_profile(ref.business_id),
        commerce.open_order(ref.business_id, customer_id) if customer_id else _none(),
    )
    spec = await spec_task

    tz = str(settings.get("timezone") or "Asia/Kolkata")
    p = biz_profile.persona
    rules = prompts.render(
        "customer",
        business_name=str(settings.get("name")),
        channel=conv.channel.replace("_", " "),
        persona_name=p.name or str(settings.get("name")),
        persona_tone=p.tone,
        persona_formality="use 'aap' (formal)" if p.formality == "aap" else f"formality {p.formality}",
        persona_emoji=p.emoji,
        job_description=conv.job_description or "Help customers with products, prices, delivery and orders.",
    )
    qa_lines = "\n".join(f"[qa:{q.entity_id}] Q: {q.title}\nA: {q.text}" for q in qas)
    system = "\n\n".join(
        [
            rules,
            "# BUSINESS (profile sections are referenced as profile:<section>; delivery zones as delivery_zone:<id>)",
            profile.text,
            "# DELIVERY ZONES\n" + "\n".join(f"delivery_zone:{z.id} = {z.label}" for z in biz_profile.delivery.zones),
            f"# CHANNEL\n{conv.channel} — capabilities: {conv.capabilities}",
            "# Q&A (owner's answers)\n" + (qa_lines or "(use knowledge_lookup)"),
        ]
    )

    cards = [card_json(c) for c in spec.catalog]
    customer = conv.customer
    facts = "; ".join(f"{f.get('key')}: {f.get('value')}" for f in (customer.facts if customer else [])) or "none"
    default_language = customer.language if customer and customer.language else settings.get("default_language")
    language_guess = lang.detect(inbound_text, default=str(default_language or "hi-Latn"))
    now_local = to_business_tz(utcnow(), tz)
    order_view = order.tool_view() if order else None
    open_order_block = (
        "# OPEN ORDER (in progress — use this order_id with the order tools; reference it as order:<order_id>)\n"
        + json.dumps(order_view, ensure_ascii=False)
        if order_view
        else ""
    )
    open_question = (
        {
            "code": pending.short_code,
            "question": pending.question,
            "asked": to_business_tz(pending.asked_at, tz).strftime("%d %b %H:%M"),
        }
        if pending
        else None
    )
    waiting_block = (
        f'# WAITING ON THE OWNER\nAsked at {open_question["asked"]}: "{open_question["question"]}". If the '
        "customer only asks for an update, say you are still checking with the team (action=send); do not ask the "
        "owner again."
        if open_question
        else ""
    )
    dynamic = "\n\n".join(
        [
            "# CUSTOMER\n"
            f"name: {customer.display_name if customer and customer.display_name else 'unknown'}; facts: {facts}\n"
            f"language of the latest message: {language_guess} (reply in this language and script)",
            "# CATALOG CARDS (live prices; reference variant/offering ids you use)\n" + _cards_block(cards),
            "# CONVERSATION SO FAR\n"
            + (f"summary: {conv.summary}\n" if conv.summary else "")
            + ("\n".join(_fmt_message(m, tz) for m in history) or "(new conversation)"),
            *([open_order_block] if open_order_block else []),
            *([waiting_block] if waiting_block else []),
            "# NOW — new message(s) from the customer\n" + "\n".join(_fmt_message(m, tz) for m in inbound),
            f"Current time: {now_local.strftime('%a %d %b %Y, %H:%M')} ({tz}). {_upcoming_days(now_local)}\n"
            "Reply by calling `reply`.",
        ]
    )
    return PreparedContext(
        system=system,
        dynamic=dynamic,
        conversation=conv,
        inbound=inbound,
        inbound_text=inbound_text,
        language_guess=language_guess,
        settings=settings,
        profile_version=profile.version,
        speculative_cards=cards,
        timings_ms={"prepare": int((time.perf_counter() - started) * 1000), "speculative": spec.elapsed_ms},
        open_order=order_view,
        open_question=open_question,
        recent_lines=[_fmt_message(m, tz) for m in [*history, *inbound]][-6:],
    )


async def _none() -> None:
    return None


def holding_text(settings: dict[str, Any], language: str) -> str:
    messages = settings.get("holding_messages") or {}
    return str(
        messages.get(language)
        or messages.get("hi-Latn")
        or messages.get("en")
        or "Ek minute, main check karke batati hoon 🙏"
    )


def customer_id_of(ctx: PreparedContext) -> UUID | None:
    return ctx.conversation.customer.id if ctx.conversation.customer else None


def _upcoming_days(now_local: datetime) -> str:
    """Resolve relative dates for the model ("kal", "parso", "Saturday"): models guess the year otherwise."""
    days = [now_local.date() + timedelta(days=i) for i in range(8)]
    names = ["today", "tomorrow", "day after tomorrow"]
    return "Dates: " + "; ".join(
        f"{names[i] if i < len(names) else d.strftime('%A')} = {d.strftime('%a %d %b')} ({d.isoformat()})"
        for i, d in enumerate(days)
    )
