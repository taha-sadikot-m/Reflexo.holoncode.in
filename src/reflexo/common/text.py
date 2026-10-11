"""Unicode-safe text normalisation shared by option mapping, opt-out detection and search."""

from __future__ import annotations

import re
import unicodedata

_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    r"""NFKC, casefold, drop punctuation/symbols, collapse whitespace.

    Unlike `re.sub(r"[^\w\s]", "", s)` this keeps combining marks, so Devanagari words such as "बंद" survive.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in folded)
    return _SPACE.sub(" ", kept).strip()
