"""The LiveKit voice agent (POC_04 §10.3–10.4): shared tools, a sentence-level price gate, transcript capture.

Tools wrap the shared registry (desk `voice`), so catalogue answers come from the same live rows as chat.
Idempotency keys are `call:{call_session_id}:{seq}:{i}`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from typing import Any

from livekit.agents import Agent, ModelSettings, RunContext, function_tool, get_job_context, llm

import reflexo.agent.api as agent_api
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit
from reflexo.voice import gate, sessions
from reflexo.voice.context import VoiceCtx

log = get_logger(__name__)
GOODBYE = {
    "hi-Latn": "Dhanyavaad! Aapka din shubh ho.",
    "hi": "धन्यवाद! आपका दिन शुभ हो।",
    "en": "Thank you for calling. Have a lovely day!",
}


class ReflexoVoiceAgent(Agent):
    def __init__(self, ctx: VoiceCtx) -> None:
        super().__init__(instructions=ctx.instructions)
        self.ctx = ctx

    async def on_enter(self) -> None:
        await self.session.say(self.ctx.greeting, allow_interruptions=True)

    # ---------------------------------------------------------------- voice gate
    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[Any]:
        stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for chunk in gate.gated(stream, self.ctx.allowed_paise, self.ctx.language):
            yield chunk

    # ---------------------------------------------------------------- shared tools
    async def _run(self, name: str, args: dict[str, Any]) -> str:
        ctx = self.ctx
        tool_ctx = agent_api.ToolContext(
            business_id=ctx.business_id,
            desk="voice",
            channel="voice",
            conversation_id=ctx.conversation_id,
            customer_id=ctx.customer_id,
            member_id=ctx.member_id,
            turn_id=None,
            call_session_id=ctx.call_session_id,
            step=ctx.next_step(),
        )
        result = await agent_api.run_tool(
            ToolCall(id=new_id().hex, name=name, args=args), tool_ctx, set(agent_api.VOICE_TOOLS)
        )
        text = json.dumps(result, ensure_ascii=False, default=str)
        ctx.allowed_paise |= {a.paise for a in agent_api.extract_amounts(text) if a.explicit}
        return text

    @function_tool()
    async def catalog_search(self, context: RunContext, query: str, max_price_rupees: int | None = None) -> str:  # type: ignore[type-arg]
        """Search the catalogue for products and their live prices. Use for any product or price question.

        Args:
            query: what the caller wants, in their words
            max_price_rupees: budget limit in rupees, if they gave one
        """
        args: dict[str, Any] = {"query": query}
        if max_price_rupees:
            args["max_price_rupees"] = max_price_rupees
        return await self._run("catalog_search", args)

    @function_tool()
    async def product_details(self, context: RunContext, offering_id: str) -> str:  # type: ignore[type-arg]
        """Full details of one product (description, eggless option, flavours, all sizes and prices).

        Args:
            offering_id: offering_id from a catalog_search result
        """
        return await self._run("catalog_get", {"offering_id": offering_id})

    @function_tool()
    async def knowledge_lookup(self, context: RunContext, query: str) -> str:  # type: ignore[type-arg]
        """Look up policies, hours, payments and the owner's saved answers.

        Args:
            query: the caller's question
        """
        return await self._run("knowledge_lookup", {"query": query})

    @function_tool()
    async def delivery_quote(self, context: RunContext, area_or_pincode: str) -> str:  # type: ignore[type-arg]
        """Whether we deliver to an area or pincode, the fee and the notice needed.

        Args:
            area_or_pincode: area name or 6-digit pincode
        """
        return await self._run("delivery_quote", {"area_or_pincode": area_or_pincode})

    @function_tool()
    async def end_call(self, context: RunContext, reason: str = "done") -> str:  # type: ignore[type-arg]
        """End the call after saying goodbye, when the caller has nothing else.

        Args:
            reason: why the call is ending (done, caller_request, ...)
        """
        await self.session.say(GOODBYE.get(self.ctx.language, GOODBYE["hi-Latn"]), allow_interruptions=False)
        await emit(
            "voice_turn",
            PipelineEventStatus.OK,
            business_id=self.ctx.business_id,
            call_session_id=str(self.ctx.call_session_id),
            step="end_call",
            reason=reason,
        )
        job = get_job_context()
        asyncio.get_running_loop().call_later(1.5, job.shutdown, f"agent_end:{reason}")
        return "call ending"


async def record_turn(ctx: VoiceCtx, role: str, text: str) -> None:
    try:
        await sessions.append_transcript(ctx.call_session_id, role, text)
    except Exception:  # the transcript must never break the call
        log.exception("voice_transcript_append_failed")
