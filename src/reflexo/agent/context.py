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

import reflexo.approvals.api as approvals
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent import language as lang
from reflexo.agent import prompts
from reflexo.agent.capabilities import CAPABILITIES
from reflexo.agent.proposals import decided_block
from reflexo.agent.tools.customer import card_json
from reflexo.agent.tools.orders import STATUS_MEANING
from reflexo.common import ports
from reflexo.common.contracts import ShopProceduresPort, TurnRef
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
    relay: dict[str, Any] | None = None  # relay turn: the owner's answer to pass on (question, answer, qa id)
    # What the business/system told the model this turn (profile, zones, Q&A, cards, open order without the
    # customer's own words, the owner's relayed answer). The output gate trusts amounts from here; never from
    # the customer's messages or from the rules prompt's illustrative examples.
    trusted_facts: str = ""


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
        relay_row = None
        if ref.hints.get("relay_question_id"):
            relay_row = (
                await s.execute(
                    text("""SELECT id, question, answer, qa_entry_id FROM owner_questions
                            WHERE id = CAST(:q AS uuid) AND status IN ('answered', 'relayed')"""),
                    {"q": ref.hints["relay_question_id"]},
                )
            ).first()
        pending = (
            await s.execute(
                text("""SELECT short_code, question, asked_at FROM owner_questions
                        WHERE conversation_id = :c AND status = 'open' ORDER BY asked_at DESC LIMIT 1"""),
                {"c": ref.conversation_id},
            )
        ).first()
    return await build_context(
        ref.business_id,
        conv,
        inbound,
        history,
        relay_row=relay_row,
        pending=pending,
        started=started,
        shop_blocks=await _shop_blocks(ref, conv.customer.id if conv.customer else None),
    )


async def build_context(
    business_id: UUID,
    conv: gateway.ConversationView,
    inbound: list[gateway.MessageView],
    history: list[gateway.MessageView],
    *,
    relay_row: Any = None,
    pending: Any = None,
    shop_blocks: list[str] | None = None,
    started: float | None = None,
) -> PreparedContext:
    """Lay out the prompt from loaded rows. Also used without a real conversation (onboarding preview sandbox)."""
    started = started if started is not None else time.perf_counter()
    inbound_text = "\n".join(m.text for m in inbound if m.text).strip()
    customer_id = conv.customer.id if conv.customer else None

    # speculative retrieval starts first so its two queries get pool connections before the other loads
    spec_task = asyncio.create_task(knowledge.speculative(business_id, inbound_text, customer_id=customer_id))
    await asyncio.sleep(0)
    profile, settings, qas, biz_profile, order = await asyncio.gather(
        knowledge.profile_block(business_id),
        knowledge.business_settings(business_id),
        knowledge.qa_block(business_id),
        knowledge.get_profile(business_id),
        commerce.open_order(business_id, customer_id) if customer_id else _none(),
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
    order_view = {**order.tool_view(), "status_meaning": STATUS_MEANING.get(order.status)} if order else None
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
    relay = (
        {
            "question_id": str(relay_row.id),
            "question": relay_row.question,
            "answer": relay_row.answer,
            "qa_id": str(relay_row.qa_entry_id),
        }
        if relay_row is not None
        else None
    )
    relay_block = (
        "# THE OWNER ANSWERED — pass it on now\n"
        f'The customer asked: "{relay["question"]}"\nThe owner\'s answer (saved as qa:{relay["qa_id"]}): '
        f'"{relay["answer"]}"\nTell the customer this answer in their language, naturally and briefly '
        f"(action=send, reference qa:{relay['qa_id']}). Continue the conversation if they asked more."
        if relay
        else ""
    )
    waiting_block = (
        f'# WAITING ON THE OWNER\nAsked at {open_question["asked"]}: "{open_question["question"]}". This covers only '
        "that question: if the customer asks about it again, say you are still checking with the team (action=send) "
        "and do not ask the owner again. Anything else in their message — a new order, another question — you "
        "handle now as usual."
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
            *([relay_block] if relay_block else []),
            *(shop_blocks or []),
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
        relay=relay,
        recent_lines=[_fmt_message(m, tz) for m in [*history, *inbound]][-6:],
        trusted_facts="\n".join(
            [
                profile.text,
                "\n".join(z.label for z in biz_profile.delivery.zones),
                qa_lines,
                json.dumps(cards, ensure_ascii=False),
                json.dumps(without_customer_words(order_view), ensure_ascii=False) if order_view else "",
                relay["answer"] if relay else "",
                f"{now_local.strftime('%a %d %b %Y, %H:%M')} {_upcoming_days(now_local)}",
            ]
        ),
    )


def without_customer_words(value: Any) -> Any:
    """A tool/order payload minus the parts that echo the customer's own input (commerce.CUSTOMER_ENTERED)."""
    if isinstance(value, dict):
        return {k: without_customer_words(v) for k, v in value.items() if k not in commerce.CUSTOMER_ENTERED}
    if isinstance(value, list):
        return [without_customer_words(v) for v in value]
    return value


async def _shop_blocks(ref: TurnRef, customer_id: UUID | None) -> list[str]:
    """The agent's awareness (ADR-015, POC_10 §7), so it never contradicts the shop:
    - HOW THIS SHOP WORKS: the owner's playbook rules and active procedures, in the words the owner approved;
    - OPEN WITH THIS CUSTOMER: proposals the owner has not answered and procedures waiting for this customer;
    - THE SHOP DECIDED: on a relay turn, the owner's decision to pass on."""
    blocks: list[str] = []
    rules = await knowledge.playbook.rules(ref.business_id, customer_id=customer_id)
    procedures = ports.registry.optional(ShopProceduresPort)  # type: ignore[type-abstract]
    how = [
        f"- {r.readback(CAPABILITIES[r.capability].label)}" + (" (this customer only)" if r.customer_id else "")
        for r in rules
        if r.capability in CAPABILITIES
    ]
    if procedures is not None:
        how += [f"- {line}" for line in await procedures.describe(ref.business_id)]
    if how:
        blocks.append(
            "# HOW THIS SHOP WORKS (the owner's own rules — act by them, never contradict them)\n" + "\n".join(how)
        )

    if proposal_id := ref.hints.get("proposal_id"):
        a = await approvals.get(ref.business_id, UUID(str(proposal_id)))
        if a is not None:
            blocks.append(decided_block(a, await approvals.outcome(ref.business_id, a.id)))
    open_items = [
        f"- You asked the owner: {a.preview.get('label', 'an action').lower()} {a.preview.get('summary', '')} — no "
        "answer yet. If the customer asks, the shop is deciding and you will update them; never say it is done."
        for a in await approvals.open_for(ref.business_id, kind="tool_call", conversation_id=ref.conversation_id)
        if str(a.id) != str(proposal_id)
    ]
    if procedures is not None:
        open_items += [f"- {line}" for line in await procedures.waiting_on(ref.business_id, customer_id)]
    if open_items:
        blocks.append("# OPEN WITH THIS CUSTOMER\n" + "\n".join(open_items))
    return blocks


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
