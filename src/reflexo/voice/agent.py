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
import reflexo.gateway.api as gateway
from reflexo.common.db import get_db
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
        self.in_call: Any = None  # InCallChat, set by the worker (photos/text sent in the chat during the call)

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

    # ---------------------------------------------------------------- orders by voice
    @function_tool()
    async def order_start(
        self,
        context: RunContext,  # type: ignore[type-arg]
        offering_id: str,
        variant_id: str | None = None,
        quantity: int = 1,
        delivery_date: str | None = None,
        delivery_slot: str | None = None,
        address: str | None = None,
        eggless: str | None = None,
        message_on_cake: str | None = None,
    ) -> str:
        """Start the caller's order with what they already said. Returns totals and the details still needed.

        Args:
            offering_id: offering_id from catalog_search
            variant_id: variant_id of the chosen size (required if there are several sizes)
            quantity: how many
            delivery_date: YYYY-MM-DD
            delivery_slot: e.g. 4pm-8pm
            address: delivery area/pincode or 'pickup'
            eggless: yes or no
            message_on_cake: text to write on the cake
        """
        item: dict[str, Any] = {"offering_id": offering_id, "quantity": quantity}
        if variant_id:
            item["variant_id"] = variant_id
        return await self._run(
            "order_start",
            {
                "items": [item],
                "details": _details(
                    delivery_date=delivery_date,
                    delivery_slot=delivery_slot,
                    address=address,
                    eggless=eggless,
                    message_on_cake=message_on_cake,
                ),
            },
        )

    @function_tool()
    async def order_update(
        self,
        context: RunContext,  # type: ignore[type-arg]
        order_id: str,
        delivery_date: str | None = None,
        delivery_slot: str | None = None,
        address: str | None = None,
        eggless: str | None = None,
        message_on_cake: str | None = None,
    ) -> str:
        """Set details on the caller's order (date YYYY-MM-DD, slot, address, eggless, cake message).

        Args:
            order_id: order_id from order_start
            delivery_date: YYYY-MM-DD
            delivery_slot: e.g. 4pm-8pm
            address: delivery area/pincode or 'pickup'
            eggless: yes or no
            message_on_cake: text to write on the cake
        """
        return await self._run(
            "order_update",
            {
                "order_id": order_id,
                "details": _details(
                    delivery_date=delivery_date,
                    delivery_slot=delivery_slot,
                    address=address,
                    eggless=eggless,
                    message_on_cake=message_on_cake,
                ),
            },
        )

    @function_tool()
    async def order_confirm(self, context: RunContext, order_id: str, payment_method: str = "razorpay_link") -> str:  # type: ignore[type-arg]
        """Confirm the order after the caller agreed to the read-back. For online payment the payment link is sent
        to their WhatsApp chat automatically — tell them it's there.

        Args:
            order_id: order_id from order_start
            payment_method: razorpay_link (UPI/card link) or cod (cash on delivery, if they ask)
        """
        raw = await self._run("order_confirm", {"order_id": order_id, "payment_method": payment_method})
        try:
            data = json.loads(raw).get("data") or {}
        except ValueError:
            return raw
        ref = data.get("reference") or {}
        if data.get("status") == "awaiting_payment" and ref.get("type") == "payment_link":
            await self._send_blocks([{"type": "payment_link", "payment_link_id": ref["id"]}], f"link:{ref['id']}")
            return raw + "\n(The payment link is now in the customer's WhatsApp chat. Tell them to pay there.)"
        return raw

    @function_tool()
    async def order_status(self, context: RunContext, order_number: str | None = None) -> str:  # type: ignore[type-arg]
        """The caller's latest (or named) order: status, payment, delivery.

        Args:
            order_number: e.g. MB-1042; omit for the latest
        """
        return await self._run("order_status", {"order_number": order_number} if order_number else {})

    # ---------------------------------------------------------------- the WhatsApp chat during the call
    @function_tool()
    async def send_to_chat(
        self,
        context: RunContext,  # type: ignore[type-arg]
        what: str,
        ref: str | None = None,
        text: str | None = None,
    ) -> str:
        """Send something to the caller's WhatsApp chat while you keep talking: a product card, an order summary
        or a short text (an address, a price you already said).

        Args:
            what: product | order_summary | text
            ref: offering_id for product, order_id for order_summary
            text: the message, for what=text
        """
        if what == "product" and ref:
            block: dict[str, Any] = {"type": "product", "offering_id": ref}
        elif what == "order_summary" and ref:
            block = {"type": "order_summary", "order_id": ref}
        elif what == "text" and text:
            if not gate.check_sentence(text, self.ctx.allowed_paise):
                return "not sent: the text has a price you haven't looked up in this call"
            block = {"type": "text", "text": text}
        else:
            return "not sent: use what=product/order_summary with ref, or what=text with text"
        await self._send_blocks([block], f"chat:{self.ctx.next_step()}")
        return "sent to the customer's WhatsApp chat"

    @function_tool()
    async def request_chat_media(self, context: RunContext, prompt: str, timeout_s: int = 60) -> str:  # type: ignore[type-arg]
        """Ask the caller to send a photo (e.g. of the cake design they want) in the WhatsApp chat, and wait for it.
        Returns what the photo shows and the closest catalogue items, or 'timeout'.

        Args:
            prompt: the chat message asking for the photo, in the caller's language
            timeout_s: how long to wait (seconds)
        """
        if self.in_call is None:
            return "unavailable"
        waiter = self.in_call.next_media()
        await self._send_blocks([{"type": "text", "text": prompt}], f"ask_media:{self.ctx.next_step()}")
        try:
            return str(await asyncio.wait_for(waiter, timeout=max(10, min(timeout_s, 120))))
        except TimeoutError:
            return "timeout: no photo arrived — carry on with the call"

    async def _send_blocks(self, blocks: list[dict[str, Any]], key: str) -> None:
        if self.ctx.conversation_id is None:
            raise RuntimeError("no WhatsApp chat for this call")
        async with get_db().tenant_session(self.ctx.business_id) as s:
            await gateway.outbound().enqueue(
                s,
                business_id=self.ctx.business_id,
                conversation_id=self.ctx.conversation_id,
                blocks=blocks,
                origin="voice",
                idempotency_base=f"call:{self.ctx.call_session_id}:{key}",
            )

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


def _details(**values: str | None) -> dict[str, str]:
    return {k: v for k, v in values.items() if v}


async def record_turn(ctx: VoiceCtx, role: str, text: str) -> None:
    try:
        await sessions.append_transcript(ctx.call_session_id, role, text)
    except Exception:  # the transcript must never break the call
        log.exception("voice_transcript_append_failed")
