"""Voice usage metering (BETA_12 §6.3, §7): a call writes one `model_calls` row per item per 60 s of session.

The voice agent's pipeline (Deepgram speech-to-text, the Gemini voice model through LiveKit, Deepgram text-to-speech)
bypasses the model gateway for latency, so its usage comes from the agent's own metrics events. They are summed and
written every minute and once more when the call ends: `voice_llm` (tokens), `voice_stt` (audio seconds),
`voice_tts` (characters) and `voice_session` (the LiveKit agent-session time).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import reflexo.agent.api as agent
from reflexo.common.config import get_settings
from reflexo.common.costs import Usage
from reflexo.common.enums import ModelCallKind
from reflexo.common.llm import CallCtx
from reflexo.common.logging import get_logger

log = get_logger(__name__)
FLUSH_EVERY_S = 60.0


def stt_price_key() -> str:
    """The Deepgram item a streaming call is billed as, e.g. `nova-3-streaming-multi`."""
    s = get_settings()
    return f"{s.stt_model}-streaming-{'multi' if s.stt_language == 'multi' else 'mono'}"


@dataclass
class VoiceUsageMeter:
    business_id: UUID
    call_session_id: UUID
    conversation_id: UUID | None = None
    llm: Usage = field(default_factory=Usage)
    stt_seconds: Decimal = Decimal(0)
    tts_characters: int = 0
    _since: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add(self, metric: Any) -> None:
        """One LiveKit metrics event (`LLMMetrics`, `STTMetrics`, `TTSMetrics`); other kinds carry no usage."""
        kind = type(metric).__name__
        if kind == "LLMMetrics":
            self.llm = Usage(
                input_tokens=self.llm.input_tokens + int(metric.prompt_tokens or 0),
                cached_input_tokens=self.llm.cached_input_tokens + int(getattr(metric, "prompt_cached_tokens", 0) or 0),
                output_tokens=self.llm.output_tokens + int(metric.completion_tokens or 0),
            )
        elif kind == "STTMetrics":
            self.stt_seconds += Decimal(str(metric.audio_duration or 0))
        elif kind == "TTSMetrics":
            self.tts_characters += int(metric.characters_count or 0)

    async def flush(self) -> None:
        """Write the rows for the usage since the last flush (the session row always, while the call lasts)."""
        async with self._lock:
            now = time.monotonic()
            session_s = Decimal(str(round(now - self._since, 2)))
            items = [
                (ModelCallKind.VOICE_SESSION, "livekit", "agent_session", Usage(audio_seconds=session_s)),
                (ModelCallKind.VOICE_LLM, "gemini", get_settings().voice_model, self.llm),
                (ModelCallKind.VOICE_STT, "deepgram", stt_price_key(), Usage(audio_seconds=self.stt_seconds)),
                (ModelCallKind.VOICE_TTS, "deepgram", get_settings().tts_model, Usage(characters=self.tts_characters)),
            ]
            self.llm, self.stt_seconds, self.tts_characters, self._since = Usage(), Decimal(0), 0, now
        ctx = CallCtx(
            business_id=self.business_id,
            purpose="voice.call",
            conversation_id=self.conversation_id,
            run_ref=f"call:{self.call_session_id}",
        )
        for kind, provider, model, usage in items:
            if usage == Usage():
                continue
            await agent.record_usage(
                ctx,
                kind=kind,
                provider=provider,
                model=model,
                usage=usage,
                duration_ms=int(float(session_s) * 1000),
                feature="voice",
            )

    async def run(self, stop: asyncio.Event) -> None:
        """Flush every minute until the call ends, then once more."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), FLUSH_EVERY_S)
            except TimeoutError:
                pass
            await self.flush()
