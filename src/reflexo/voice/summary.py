"""End of a call (POC_00 F09): the recap in the customer's WhatsApp chat — and no promise left behind.

The recap is written from what the system recorded during the call (orders, questions to the owner, decisions
requested), not from the model's memory of the conversation (live 14 Oct: "You ordered a 1 kg Red Velvet cake"
when no order existed). The same pass checks the transcript for anything the assistant promised to do later that
no record covers; each such promise becomes a real question to the owner, so it is kept even if the agent forgot
to act on it during the call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

import reflexo.agent.api as agent_api
import reflexo.gateway.api as gateway
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.llm import CallCtx, ChatMessage
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit
from reflexo.voice import sessions
from reflexo.voice.context import VoiceCtx

log = get_logger(__name__)
LANGUAGE = {"hi-Latn": "Hinglish (Roman letters)", "hi": "Hindi (Devanagari)", "en": "English"}


class CallOutcome(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    open_promises: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Things the assistant said it would do after the call that no system record covers",
    )


async def review(ctx: VoiceCtx, transcript: list[dict[str, Any]], facts: list[str]) -> CallOutcome | None:
    """The recap from the facts, and the promises no fact covers. None for an empty call."""
    turns = [f"{t.get('role')}: {t.get('text')}" for t in transcript if t.get("text")]
    if len(turns) < 2:
        return None
    on_record = "\n".join(f"- {f}" for f in facts) or "- nothing (no order was placed, nothing was asked)"
    prompt = (
        f"A phone call between {ctx.business_name}'s assistant and a customer has ended.\n\n"
        f"WHAT THE SYSTEM RECORDED DURING THE CALL (the only facts):\n{on_record}\n\n"
        f"TRANSCRIPT:\n" + "\n".join(turns[-60:]) + "\n\n"
        "1. open_promises: every thing the assistant told the customer it would do LATER (check with the owner, "
        "send a price/details, call back, arrange something) that is NOT covered by a recorded fact above. Write "
        "each as what the customer is waiting for, with the details from the call (item, design, date, pickup or "
        "address). Empty if every promise is on record.\n"
        f"2. summary: 1-3 short lines for the customer's WhatsApp chat, in "
        f"{LANGUAGE.get(ctx.language, 'Hinglish (Roman letters)')}. State orders ONLY as recorded (if no order was "
        "placed, do not say one was). For anything waiting on the shop (recorded questions and open_promises), "
        "say the shop will reply here on WhatsApp. No new promises, no prices that are not recorded."
    )
    result: CallOutcome = await agent_api.gateway().structured(
        "classifier",
        [ChatMessage(role="user", content=prompt)],
        CallOutcome,
        ctx=CallCtx(business_id=ctx.business_id, purpose="voice.summary"),
    )
    return result


async def keep_promises(ctx: VoiceCtx, promises: list[str]) -> int:
    """Each promise no record covers becomes a question to the owner (with the photos the customer sent)."""
    if ctx.conversation_id is None:
        return 0
    who = ctx.customer_name or "the caller"
    kept = 0
    for promise in promises:
        try:
            await agent_api.ask_owner(
                ctx.business_id,
                ctx.conversation_id,
                agent_api.OwnerQuestionDraft(question=promise[:500]),
                brief=f"On the phone I told {who} we'd get back to them about this — they are waiting for your answer.",
                facts_extra="📞 promised during a phone call",
            )
            kept += 1
        except Exception:
            log.exception("voice_promise_follow_up_failed", call_session_id=str(ctx.call_session_id))
    return kept


async def send_summary(ctx: VoiceCtx) -> None:
    if ctx.role != "customer" or ctx.conversation_id is None:
        return
    if not await sessions.claim_summary(ctx.call_session_id):
        return  # already done for this call: one recap, one follow-up per promise
    try:
        call = await sessions.get(ctx.call_session_id)
        since = call.created_at if call and call.created_at else None
        facts = await agent_api.on_record_since(ctx.business_id, ctx.conversation_id, since) if since else []
        outcome = await review(ctx, await sessions.transcript(ctx.call_session_id), facts)
        if outcome is None:
            return
        kept = await keep_promises(ctx, outcome.open_promises)
        await sessions.set_summary(ctx.call_session_id, outcome.summary)
        async with get_db().tenant_session(ctx.business_id) as s:
            await gateway.outbound().enqueue(
                s,
                business_id=ctx.business_id,
                conversation_id=ctx.conversation_id,
                blocks=[{"type": "text", "text": f"📞 Call summary: {outcome.summary}"}],
                origin="voice",
                idempotency_base=f"call:{ctx.call_session_id}:summary",
            )
        await emit(
            "voice_turn",
            PipelineEventStatus.OK,
            business_id=ctx.business_id,
            call_session_id=str(ctx.call_session_id),
            step="summary",
            facts=len(facts),
            promises_followed_up=kept,
        )
    except Exception:
        log.exception("voice_summary_failed", call_session_id=str(ctx.call_session_id))
