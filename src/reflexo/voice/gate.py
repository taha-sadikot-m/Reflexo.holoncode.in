"""Voice gate (POC_04 §10.6): the LLM text stream is checked sentence by sentence before it reaches TTS.

Every amount in a sentence must be one the tools returned during this call (or a profile delivery fee); a failing
sentence is replaced with a safe line. Uses the same Indian-format amount extractor as the chat output gate.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

import reflexo.gateway.api as gateway
from reflexo.agent.api import extract_amounts
from reflexo.common.logging import get_logger

log = get_logger(__name__)
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+|\n+")
SAFE_LINE = {
    "hi-Latn": "Iska exact price main aapko WhatsApp par bhejti hoon.",
    "hi": "इसका सही दाम मैं आपको व्हाट्सऐप पर भेजती हूँ।",
    "en": "I'll send you the exact price on WhatsApp.",
}


def check_sentence(sentence: str, allowed_paise: set[int]) -> bool:
    return all(a.paise in allowed_paise for a in extract_amounts(sentence))


def safe_line(language: str) -> str:
    return SAFE_LINE.get(language, SAFE_LINE["hi-Latn"])


def _text_of(chunk: Any) -> str | None:
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    if delta is not None and delta.content and not delta.tool_calls:
        return str(delta.content)
    return None


async def gated(stream: AsyncIterable[Any], allowed_paise: set[int], language: str) -> AsyncIterator[Any]:
    """Pass tool calls through untouched; emit text one checked sentence at a time."""
    buffer = ""

    def release(sentence: str) -> str:
        if check_sentence(sentence, allowed_paise):
            return gateway.plain_punctuation(sentence)  # house style, as for every chat message
        log.warning("voice_gate_replaced_sentence", amounts=[a.raw for a in extract_amounts(sentence)])
        return safe_line(language) + " "

    async for chunk in stream:
        text = _text_of(chunk)
        if text is None:
            if buffer.strip():
                yield release(buffer)
                buffer = ""
            yield chunk
            continue
        buffer += text
        parts = _SENTENCE_END.split(buffer)
        for sentence in parts[:-1]:
            if sentence.strip():
                yield release(sentence.strip()) + " "
        buffer = parts[-1]
    if buffer.strip():
        yield release(buffer)
