"""In-call chat loop (POC_04 §10.5, the differentiator): while the customer talks, what they send in the WhatsApp
chat reaches the voice agent — a photo is described and matched against the catalogue, typed text counts as if
spoken (addresses, spellings), a location becomes the delivery address.

Results are added to the agent's chat context before it speaks, so a barge-in does not lose them.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import reflexo.agent.api as agent_api
import reflexo.knowledge.api as knowledge
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit
from reflexo.voice import call_events
from reflexo.voice.context import VoiceCtx

log = get_logger(__name__)
POLL_S = 0.5
ACK = {
    "hi-Latn": {"image": "Photo mil gayi, ek second dekhti hoon.", "document": "File mil gayi, ek second."},
    "hi": {"image": "फ़ोटो मिल गई, एक सेकंड देखती हूँ।", "document": "फ़ाइल मिल गई, एक सेकंड।"},
    "en": {"image": "Got the photo, one second.", "document": "Got the file, one second."},
}


class InCallChat:
    def __init__(self, ctx: VoiceCtx, session: Any, agent: Any) -> None:
        self.ctx, self.session, self.agent = ctx, session, agent
        self.stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._waiters: list[asyncio.Future[str]] = []

    async def run(self) -> None:
        """Catch up on anything sent while the call was connecting, then follow new events until the call ends."""
        listener = asyncio.create_task(call_events.subscribe(self.ctx.call_session_id, self._wake, self.stop))
        try:
            while not self.stop.is_set():
                for ev in await call_events.claim_pending(self.ctx.call_session_id):
                    await self._handle(ev)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_S)
        finally:
            listener.cancel()

    def next_media(self) -> asyncio.Future[str]:
        """For `request_chat_media`: resolved with the next photo/document's result."""
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        return fut

    async def _handle(self, ev: call_events.CallEvent) -> None:
        try:
            if ev.kind in ("image", "document"):
                result = await self._media(ev)
                waiting = [f for f in self._waiters if not f.done()]
                self._waiters.clear()
                for fut in waiting:  # request_chat_media: the tool result carries it and the agent speaks from it
                    fut.set_result(result)
                if not waiting:
                    await self._add_context(result)
                    self.session.generate_reply(
                        instructions="Respond to what the customer just shared, using the matches."
                    )
            elif ev.kind == "location":
                await self._add_context(f"[Customer shared their location in the WhatsApp chat] {ev.text}")
                self.session.generate_reply(instructions="Use this as the delivery address; check delivery_quote.")
            elif ev.text:
                self.session.generate_reply(user_input=ev.text)  # typed text counts as if spoken
            await call_events.mark(ev.id, "processed")
            await emit(
                "call_event",
                PipelineEventStatus.OK,
                business_id=self.ctx.business_id,
                call_session_id=str(self.ctx.call_session_id),
                kind=ev.kind,
                step="processed",
            )
        except Exception as exc:
            log.exception("call_event_failed", call_event_id=str(ev.id))
            await call_events.mark(ev.id, "failed", {"error": type(exc).__name__})

    async def _media(self, ev: call_events.CallEvent) -> str:
        lang = self.ctx.language if self.ctx.language in ACK else "hi-Latn"
        self.session.say(ACK[lang].get(ev.kind, ACK[lang]["image"]), allow_interruptions=True)
        media_id = await call_events.wait_media(ev.message_id) if ev.message_id else None
        if media_id is None:
            return f"[Customer sent a {ev.kind} in the WhatsApp chat, but it could not be downloaded yet]"
        description = await agent_api.describe_media(self.ctx.business_id, media_id)
        cards = await knowledge.match_image_description(self.ctx.business_id, description) if ev.kind == "image" else []
        lines = []
        for c in cards[:3]:
            sizes = ", ".join(f"{v.name} {v.price_display}" for v in c.variants if v.price_paise is not None)
            lines.append(f"{c.name} (offering_id {c.offering_id}): {sizes or 'price on request'}")
            self.ctx.allowed_paise |= {v.price_paise for v in c.variants if v.price_paise is not None}
        await call_events.mark(ev.id, "delivered", {"description": description, "matches": [c.name for c in cards[:3]]})
        caption = f" Caption: {ev.text}." if ev.text and not ev.text.startswith("[") else ""
        matches = ("\nClosest catalogue items:\n" + "\n".join(lines)) if lines else "\nNo close catalogue match."
        return f"[Customer shared a {ev.kind} in the WhatsApp chat] {description}.{caption}{matches}"

    async def _add_context(self, content: str) -> None:
        chat_ctx = self.agent.chat_ctx.copy()
        chat_ctx.add_message(role="user", content=content)
        await self.agent.update_chat_ctx(chat_ctx)
