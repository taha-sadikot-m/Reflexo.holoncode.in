"""Deterministic query understanding before search (POC_05 §1.5 "Query normalization").

Turns "chocolateee truffle 1kg ka price kya hai" into search text "chocolatee truffle" + size hint "1 kg", and
"1,000 ke andar cake" into "cake" + `max_price_paise=100000`. Pure functions; no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reflexo.common.text import normalize

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_REPEATS = re.compile(r"([^\W\d_])\1{2,}")  # letters only: "chocolateee" → "chocolatee", never "1000"

_CURRENCY = r"(?:rs|inr|rupees?|rupaye|rupay|rupiya|रुपये|रुपए|रु)"
_NUM = r"(\d+(?:\.\d+)?)"
_PRICE_BEFORE = re.compile(
    rf"\b(?:under|below|less than|within|upto|up to|max|maximum|budget|andar)\s*{_CURRENCY}?\s*{_NUM}"
    rf"\s*{_CURRENCY}?(?!\s*(?:kg|g|gm|gram))"
)
_PRICE_AFTER = re.compile(
    rf"{_NUM}\s*{_CURRENCY}?\s*(?:ke andar|ke under|ke niche|ke neeche|se kam|tak|or less|or below|max"
    rf"|के अंदर|से कम|तक|के नीचे)(?=\s|$)"
)

_SIZE_WORDS = {
    "paav kilo": "250 g",
    "pav kilo": "250 g",
    "aadha kilo": "500 g",
    "adha kilo": "500 g",
    "aadha kg": "500 g",
    "adha kg": "500 g",
    "half kg": "500 g",
    "half kilo": "500 g",
    "आधा किलो": "500 g",
    "ek kilo": "1 kg",
    "ek kg": "1 kg",
    "one kg": "1 kg",
    "one kilo": "1 kg",
    "एक किलो": "1 kg",
    "dedh kilo": "1.5 kg",
    "dedh kg": "1.5 kg",
    "डेढ़ किलो": "1.5 kg",
    "do kilo": "2 kg",
    "do kg": "2 kg",
    "two kg": "2 kg",
    "दो किलो": "2 kg",
}
_SIZE_WORD_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(k) for k in sorted(_SIZE_WORDS, key=len, reverse=True)) + r")(?!\w)"
)
_KG = re.compile(rf"(?<!\w){_NUM}\s*(?:kg|kgs|kilo|kilos|kilogram|किलो)(?!\w)")
_GRAMS = re.compile(r"(?<!\w)(\d+)\s*(?:g|gm|gms|gram|grams|ग्राम)(?!\w)")

STOPWORDS = frozenset(
    {
        # English
        "a", "an", "the", "of", "for", "to", "in", "on", "is", "are", "do", "does", "you", "your", "have", "has",
        "i", "me", "my", "we", "want", "need", "please", "pls", "plz", "price", "prices", "rate", "rates", "cost",
        "how", "much", "what", "which", "any", "some", "show", "tell", "about", "available", "and", "or", "with",
        # Hinglish
        "ka", "ki", "ke", "ko", "hai", "hain", "h", "kya", "kitna", "kitne", "kitni", "kaise", "kaun", "sa", "se",
        "mein", "mujhe", "chahiye", "dikhao", "batao", "bhai", "ji", "aap", "aapke", "paas", "wala", "wali",
        "koi", "milega", "milegi", "hoga", "rupees", "rs", "karte", "karti", "karta", "karo", "ho", "hota", "hoti",
        "raha", "rahe", "tha", "bhi", "abhi", "kuch", "ya", "aur",
        # Hindi
        "का", "की", "के", "को", "है", "हैं", "क्या", "कितना", "कितने", "मुझे", "चाहिए", "में", "दाम", "कीमत",
    }
)  # fmt: skip


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    normalized: str  # full normalized text (embedding input)
    text: str  # search text without price/size phrases and filler; "" = browse by filters only
    max_price_paise: int | None = None
    size_hint: str | None = None  # canonical "500 g" / "1 kg" / "1.5 kg"

    @property
    def terms(self) -> list[str]:
        return self.text.split()


def _canonical_kg(value: float) -> str:
    if value < 1:
        return f"{round(value * 1000)} g"
    return f"{value:g} kg"


def canonical_size(text: str) -> str | None:
    """'1kg' / '1 Kg' / '1000 g' → '1 kg'; '500gm' / '0.5 kg' / 'aadha kilo' → '500 g'. None if no size."""
    t = _restore_decimals(normalize(_prepare_numbers(text)))
    word = _SIZE_WORD_RE.search(t)
    if word:
        return _SIZE_WORDS[word.group(1)]
    kg = _KG.search(t)
    if kg:
        return _canonical_kg(float(kg.group(1)))
    grams = _GRAMS.search(t)
    if grams:
        g = int(grams.group(1))
        return _canonical_kg(g / 1000) if g >= 1000 else f"{g} g"
    return None


def _prepare_numbers(text: str) -> str:
    t = text.translate(_DEVANAGARI_DIGITS)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)  # 1,000 → 1000 (before punctuation is stripped)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*[kK](?![a-zA-Z])", lambda m: str(round(float(m.group(1)) * 1000)), t)  # 1.5k
    # keep decimal points between digits through normalisation: 1.5 → 1p5 → restored below
    return re.sub(r"(?<=\d)\.(?=\d)", "p", t)


def _restore_decimals(t: str) -> str:
    return re.sub(r"(?<=\d)p(?=\d)", ".", t)


def parse_query(raw: str) -> ParsedQuery:
    t = _restore_decimals(normalize(_prepare_numbers(raw)))
    t = _REPEATS.sub(r"\1\1", t)
    normalized = t

    max_price: int | None = None
    for pattern in (_PRICE_AFTER, _PRICE_BEFORE):
        m = pattern.search(t)
        if m:
            max_price = round(float(m.group(1)) * 100)
            t = t[: m.start()] + " " + t[m.end() :]
            break

    size: str | None = None
    word = _SIZE_WORD_RE.search(t)
    if word:
        size = _SIZE_WORDS[word.group(1)]
        t = t[: word.start()] + " " + t[word.end() :]
    else:
        kg = _KG.search(t)
        grams = _GRAMS.search(t)
        if kg:
            size = _canonical_kg(float(kg.group(1)))
            t = t[: kg.start()] + " " + t[kg.end() :]
        elif grams:
            g = int(grams.group(1))
            size = _canonical_kg(g / 1000) if g >= 1000 else f"{g} g"
            t = t[: grams.start()] + " " + t[grams.end() :]

    terms = [w for w in t.split() if w not in STOPWORDS and not w.replace(".", "").isdigit()]
    text = " ".join(terms)
    if not text and max_price is None and size is None:
        text = normalized  # nothing but filler: search the whole sentence rather than nothing
    return ParsedQuery(raw=raw, normalized=normalized, text=text, max_price_paise=max_price, size_hint=size)
