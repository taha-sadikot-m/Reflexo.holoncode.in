"""Language of a customer message: English, Hindi (Devanagari) or Hinglish (Roman Hindi). Deterministic."""

from __future__ import annotations

import re

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_HINGLISH = frozenset(
    "hai hain kya kaise kitna kitne kitni ka ki ke ko mein mujhe chahiye aap aapka hum tum nahi nahin haan ha ji "
    "bhai karo karna kar raha rahi tha thi kab kahan kaun kyun accha acha theek thik bhejo batao dena milega "
    "wala wali abhi kal aaj bahut thoda sab koi kuch".split()
)


def detect(text: str | None, default: str = "hi-Latn") -> str:
    if not text or not text.strip():
        return default
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if _DEVANAGARI.match(c)) / len(letters) > 0.3:
        return "hi"
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return default
    hits = sum(1 for w in words if w in _HINGLISH)
    return "hi-Latn" if hits >= 1 and hits / len(words) >= 0.15 else "en"
