"""End-of-call summary (POC_00 F09): a short recap in the customer's WhatsApp chat after the call."""

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


class Recap(BaseModel):
    summary: str = Field(min_length=1, max_length=400)


async def summarize(ctx: VoiceCtx, transcript: list[dict[str, Any]]) -> str | None:
    """1–3 lines from the transcript (classifier role) in the caller's language; None for an empty call."""
    turns = [f"{t.get('role')}: {t.get('text')}" for t in transcript if t.get("text")]
    if len(turns) < 2:
        return None
    prompt = (
        f"Summarise this phone call between {ctx.business_name} and a customer in 1-3 short lines for the "
        f"customer's WhatsApp chat, in {LANGUAGE.get(ctx.language, 'Hinglish (Roman letters)')}: what they asked, "
        "what was decided (order, prices said, next steps). Only facts from the call; no new promises.\n\n"
        + "\n".join(turns[-40:])
    )
    recap = await agent_api.gateway().structured(
        "classifier",
        [ChatMessage(role="user", content=prompt)],
        Recap,
        ctx=CallCtx(business_id=ctx.business_id, purpose="voice.summary"),
    )
    return f"📞 Call summary: {recap.summary}"


async def send_summary(ctx: VoiceCtx) -> None:
    if ctx.role != "customer" or ctx.conversation_id is None:
        return
    try:
        text_ = await summarize(ctx, await sessions.transcript(ctx.call_session_id))
        if not text_:
            return
        async with get_db().tenant_session(ctx.business_id) as s:
            await gateway.outbound().enqueue(
                s,
                business_id=ctx.business_id,
                conversation_id=ctx.conversation_id,
                blocks=[{"type": "text", "text": text_}],
                origin="voice",
                idempotency_base=f"call:{ctx.call_session_id}:summary",
            )
        await emit(
            "voice_turn",
            PipelineEventStatus.OK,
            business_id=ctx.business_id,
            call_session_id=str(ctx.call_session_id),
            step="summary",
        )
    except Exception:
        log.exception("voice_summary_failed", call_session_id=str(ctx.call_session_id))
