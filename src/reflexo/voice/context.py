"""Voice session context (POC_04 §10.2 step 2): what the voice agent knows when it joins a call."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any
from uuid import UUID

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.xray import emit
from reflexo.voice import sessions

_PROMPT = Template((Path(__file__).resolve().parent / "prompt.md").read_text(encoding="utf-8"))


@dataclass
class VoiceCtx:
    call_session_id: UUID
    business_id: UUID
    customer_id: UUID | None
    member_id: UUID | None
    conversation_id: UUID | None
    caller_e164: str
    role: str  # customer | owner | team
    business_name: str
    customer_name: str | None
    language: str
    greeting: str
    instructions: str
    allowed_paise: set[int] = field(default_factory=set)  # amounts tools returned during this call (voice gate)
    tool_seq: int = 0
    started: float = field(default_factory=time.monotonic)

    def next_step(self) -> int:
        self.tool_seq += 1
        return self.tool_seq


class SessionNotLive(Exception):
    pass


def _greeting(
    settings: dict[str, Any], persona_greeting: str | None, business_name: str, role: str, customer_name: str | None
) -> str:
    if role in ("owner", "team"):
        name = f" {customer_name}" if customer_name else ""
        return f"Namaste{name} ji! {business_name} ka assistant bol raha hoon. Bataiye, kya karna hai?"
    if persona_greeting:
        spoken = "".join(ch for ch in persona_greeting if ch.isalnum() or ch.isspace() or ch in ".,!?'-").strip()
        if spoken and spoken[-1] not in ".!?":
            spoken += "."
        return f"{spoken} Bataiye, kya chahiye?"
    return f"Namaste, {business_name} se bol rahi hoon. Bataiye, kya chahiye?"


async def start_session(call_session_id: UUID) -> VoiceCtx:
    """Called by the voice agent when its job starts (the bridge created the session and dispatched the agent)."""
    session = await sessions.get(call_session_id)
    if session is None or session.status not in sessions.LIVE:
        raise SessionNotLive(f"call session {call_session_id} is not live")
    settings = await knowledge.business_settings(session.business_id)
    profile = await knowledge.get_profile(session.business_id)
    block = await knowledge.profile_block(session.business_id)
    customer_name: str | None = None
    language = str(settings.get("default_language") or "hi-Latn")
    facts = "none"
    if session.conversation_id is not None:
        async with get_db().tenant_session(session.business_id) as s:
            conv = await gateway.conversation_view(s, session.conversation_id)
        if conv and conv.customer:
            customer_name = conv.customer.display_name
            language = conv.customer.language or language
            facts = "; ".join(f"{f.get('key')}: {f.get('value')}" for f in conv.customer.facts) or "none"
    role = "owner" if session.member_id is not None else "customer"
    business_name = str(settings.get("name"))
    instructions = _PROMPT.substitute(
        business_name=business_name,
        persona_name=profile.persona.name or business_name,
        formality="use 'aap' (formal)" if profile.persona.formality == "aap" else profile.persona.formality,
        caller=("the OWNER of the business" if role == "owner" else f"a customer ({customer_name or 'name unknown'})"),
        language=language,
        facts=facts,
        profile=block.text,
    )
    ctx = VoiceCtx(
        call_session_id=session.id,
        business_id=session.business_id,
        customer_id=session.customer_id,
        member_id=session.member_id,
        conversation_id=session.conversation_id,
        caller_e164=session.caller_e164,
        role=role,
        business_name=business_name,
        customer_name=customer_name,
        language=language,
        greeting=_greeting(settings, profile.persona.greeting, business_name, role, customer_name),
        instructions=instructions,
    )
    # delivery fees/thresholds from the profile are facts the agent may say without a tool call
    for z in profile.delivery.zones:
        ctx.allowed_paise.add(z.fee_paise)
        if z.free_above_paise:
            ctx.allowed_paise.add(z.free_above_paise)
    await emit(
        "voice_turn",
        PipelineEventStatus.START,
        business_id=session.business_id,
        conversation_id=session.conversation_id,
        call_session_id=str(session.id),
        role=role,
    )
    return ctx
