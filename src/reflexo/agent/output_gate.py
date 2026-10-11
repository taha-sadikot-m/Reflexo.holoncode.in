"""Output gate (POC_04 §5): deterministic checks on what the customer desk is about to send. Target < 10 ms.

Harvested from the old `agent/guardrails.py` (H12: currency regex, phone matching), extended to Indian formats:
₹/Rs/INR/rupees/rupaye/रु/रुपये, `/-`, Indian digit grouping (1,10,000), `k` suffix, Devanagari digits, and bare
numbers next to price words. Every amount must equal an amount in a referenced record; discounts, links and
contact details must be backed; grounded topics need references; tier rules may rewrite the action.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from reflexo.agent.schemas import (
    GROUNDED_TOPICS,
    HANDOFF_TOPICS,
    AgentOutput,
    GateCheck,
    GateResult,
    GroundingRecords,
)
from reflexo.common.blocks import (
    ButtonsBlock,
    CarouselBlock,
    FileBlock,
    ImageBlock,
    ListBlock,
    ProductBlock,
    TextBlock,
)
from reflexo.common.money import format_inr

_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
_NUM = r"(\d{1,3}(?:,\d{2})*,\d{3}|\d+)(?:\.(\d{1,2}))?\s*([kK]\b)?"
_CUR_WORDS = r"(?:₹|rs\.?|inr|rupees?|rupaye|rupay|rupiya|rupiye|रु\.?|रुपये|रुपए|रुपया)"
_CUR_BEFORE = re.compile(rf"(?<![\w]){_CUR_WORDS}\s*{_NUM}", re.IGNORECASE)
_CUR_AFTER = re.compile(rf"(?<![\d,.]){_NUM}\s*(?:/-|{_CUR_WORDS}(?![\w]))", re.IGNORECASE)
_BARE = re.compile(rf"(?<![\d,.:/₹]){_NUM}(?![\d,.:])", re.IGNORECASE)

PRICE_WORDS = frozenset(
    {
        "price",
        "prices",
        "cost",
        "costs",
        "daam",
        "dam",
        "kitne",
        "kitna",
        "total",
        "rate",
        "amount",
        "charge",
        "charges",
        "fee",
        "fees",
        "only",
        "sirf",
        "bas",
        "ka",
        "ki",
        "ke",
        "का",
        "की",
        "के",
        "कीमत",
        "दाम",
        "मूल्य",
    }
)
UNITS = re.compile(
    r"^\s*(?:g|gm|gms|gram|grams|kg|kgs|kilo|kilos|ग्राम|किलो|pcs|pc|piece|pieces|slice|slices|inch|inches|cm|km|"
    r"%|am|pm|hrs?|hours?|h|days?|din|ghante|minutes?|mins?|people|log|persons?|serves|ml|l|litres?|x|×|"
    r"jar|box|boxes|cupcakes?|cookies|brownies|nos?)\b",
    re.IGNORECASE,
)
# Discount claims (whole words only: "egg*less* 1 kg" is not a discount). Strong words count in either direction;
# "off" only after an amount ("₹100 off", never "we're off on 15th"); weak words ("less", "kam", "offer") only right
# after a rupee amount ("₹100 less", "50 rupees kam"), so "less than 24 hours" / "offer 2 options" stay clean.
_STRONG = r"(?:discount|chhoot|chhut|chut|choot|छूट|concession)"
_WEAK = r"(?:less|kam|kam kar|kam karke|offer)"
_GAP = r"[ :,-]+(?:[\wऀ-ॿ]+[ :,-]+){0,2}?"
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|pratishat|प्रतिशत)", re.IGNORECASE)
# within one sentence: words separated by spaces/commas only, never across "." / "?" / newline
_DISCOUNT_NEAR = re.compile(
    rf"(?<![\wऀ-ॿ]){_STRONG}{_GAP}(?:₹|rs\.?)?\s*(\d[\d,]*)"
    rf"|(?<![\w.])(?:₹|rs\.?)?\s*(\d[\d,]*)\s*(?:rs\.?|rupees?|rupaye|₹)?{_GAP}(?:{_STRONG}|off)(?![\wऀ-ॿ])"
    rf"|(?:(?:₹|rs\.?)\s*(\d[\d,]*)|(?<![\w.])(\d[\d,]*)\s*(?:rs\.?|rupees?|rupaye|₹))[ :,-]+{_WEAK}(?![\wऀ-ॿ])",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:no|not|nahi|nahin|nhi|without)\b|नहीं|नही", re.IGNORECASE)
_BUDGET_AFTER = re.compile(
    r"^\s*(?:ke andar|ke under|ke niche|ke neeche|se kam|tak|or less|or below|budget|के अंदर|से कम|तक)(?![\w])",
    re.IGNORECASE,
)
_BUDGET_BEFORE = re.compile(r"\b(?:under|below|within|upto|up to|budget|less than)\s*(?:₹|rs\.?)?\s*$", re.IGNORECASE)
_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"')]+|\b(?:[a-z0-9-]+\.)+(?:com|in|io|me|net|org|co|shop|store|link|app)\b(?:/[^\s<>\"')]*)?",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\d])(?:\+?\d[\d\s-]{8,}\d)(?![\d])")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\.\d{2}\s*(?:am|pm)\b", re.IGNORECASE)
# Indian PIN code (411038 / 411 038). Live: "Kothrud 411038 mein. Iska total ₹1,100" read the PIN as a price.
_PIN = re.compile(r"(?<!\d)(?<!\d[,.])[1-9]\d{2}\s?\d{3}(?!\d|[,.]\d)")
# Reference codes: order numbers "MB-1012", "#1012", invoice "INV2041". Live: "order MB-1012 … Total ₹1,100".
_CODE = re.compile(r"(?:\b[A-Za-z]{1,6}-?|#)\d+\b")
# "Your order is confirmed" / "order confirm ho gaya" / "बुक हो गया" — a claim, not a question ("confirm kar doon?")
# or a future ("confirm ho jayega", "once confirmed"). Live: a draft was announced as "order confirm ho gaya hai".
_CONFIRMED_CLAIM = re.compile(
    r"\b(?:is|has been|was|got|now)\s+(?:been\s+)?(?:confirmed|booked)\b"
    r"|\b(?:order|booking)\s+(?:confirmed|booked)\b"
    r"|\b(?:confirm(?:ed)?|book|place)\s+(?:ho\s*(?:gaya|gayi|gya|chuka|chuki)|hogaya|hogya|kar\s*(?:diya|di|dia))\b"
    r"|(?:कन्फर्म|बुक|पक्का)\s*(?:हो\s*(?:गया|गई|चुका)|कर\s*(?:दिया|दी))",
    re.IGNORECASE,
)
# "…is confirmed once paid" (what the prompt asks for after order_confirm) is a condition, not a claim.
_CONDITION_AFTER = re.compile(
    r"^\W*(?:once|after|when|as soon as|upon|if|jab|payment\s+(?:ke\s+baad|hote\s+hi|milte\s+hi|hone\s+par))\b",
    re.IGNORECASE,
)
# Hindi word order puts the condition first: "payment ke baad booking confirmed".
_CONDITION_BEFORE = re.compile(
    r"(?:\b(?:once|after|when|upon|if|jab)\b|ke\s+baad|hone\s+par|hote\s+hi|milte\s+hi|के\s+बाद)[^.!?\n]*$",
    re.IGNORECASE,
)
PAYMENT_HOSTS = frozenset({"rzp.io", "razorpay.com", "pages.razorpay.com"})


@dataclass(frozen=True)
class Amount:
    rupees: float
    raw: str
    explicit: bool  # had a currency marker

    @property
    def paise(self) -> int:
        return round(self.rupees * 100)


def _value(integer: str, decimals: str | None, k: str | None) -> float:
    v = float(integer.replace(",", "") + (f".{decimals}" if decimals else ""))
    return v * 1000 if k else v


def _span_overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < e and s < span[1] for s, e in taken)


def _is_budget(t: str, start: int, end: int) -> bool:
    """'1000 ke andar', 'under ₹1000': the customer's budget restated, not a price claim."""
    return bool(_BUDGET_AFTER.match(t[end:]) or _BUDGET_BEFORE.search(t[max(0, start - 20) : start]))


def extract_amounts(text: str, quantity_numbers: Iterable[int] = ()) -> list[Amount]:
    """Money mentions in `text` (explicit currency, or a bare number within 3 tokens of a price word)."""
    t = text.translate(_DEVANAGARI)
    found: list[Amount] = []
    taken: list[tuple[int, int]] = [m.span() for m in _TIME.finditer(t)] + [m.span() for m in _PHONE.finditer(t)]
    taken += [m.span() for m in _URL.finditer(t)] + [m.span() for m in _PCT.finditer(t)]
    for pattern in (_CUR_BEFORE, _CUR_AFTER):
        for m in pattern.finditer(t):
            if _span_overlaps(m.span(), taken):
                continue
            taken.append(m.span())
            if _is_budget(t, m.start(), m.end()):
                continue
            found.append(Amount(_value(m.group(1), m.group(2), m.group(3)), m.group(0).strip(), True))
    for pattern in (_PIN, _CODE):  # after explicit ₹ amounts, so "₹411038" / "Rs500" stay amounts
        taken += [m.span() for m in pattern.finditer(t) if not _span_overlaps(m.span(), taken)]
    quantities = set(quantity_numbers)
    for m in _BARE.finditer(t):
        if _span_overlaps(m.span(), taken):
            continue
        value = _value(m.group(1), m.group(2), m.group(3))
        if value < 10 or (value == int(value) and int(value) in quantities):
            continue
        if UNITS.match(t[m.end() :]) or _is_budget(t, m.start(), m.end()):
            continue
        before = re.findall(r"[\wऀ-ॿ]+", t[max(0, m.start() - 40) : m.start()])[-3:]
        after = re.findall(r"[\wऀ-ॿ]+", t[m.end() : m.end() + 40])[:3]
        if any(w.lower() in PRICE_WORDS for w in before + after):
            found.append(Amount(value, m.group(0).strip(), False))
    return found


def output_texts(output: AgentOutput) -> list[str]:
    texts: list[str] = []
    for b in output.blocks:
        if isinstance(b, TextBlock):
            texts.append(b.text)
        elif isinstance(b, ButtonsBlock | ListBlock):
            texts.append(b.text)
            texts += [o.title for o in b.options]
        elif isinstance(b, ImageBlock | FileBlock) and b.caption:
            texts.append(b.caption)
    return texts


def _block_offerings(output: AgentOutput) -> set[str]:
    ids: set[str] = set()
    for b in output.blocks:
        if isinstance(b, ProductBlock):
            ids.add(str(b.offering_id))
        elif isinstance(b, CarouselBlock):
            ids |= {str(i.offering_id) for i in b.items}
    return ids


def _host(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _phone_allowed(raw: str, allowed: set[str]) -> bool:
    d = _digits(raw)
    return any(d == a or (len(d) >= 10 and len(a) >= 10 and d[-10:] == a[-10:]) for a in allowed)


def check(output: AgentOutput, records: GroundingRecords) -> GateResult:
    started = time.perf_counter()
    checks: list[GateCheck] = []
    text = "\n".join(output_texts(output))
    sending = output.action in ("send", "handoff")

    # 1. references exist and belong to this business
    unknown_blocks = _block_offerings(output) - records.block_offering_ids
    if records.missing_refs or unknown_blocks:
        detail = ", ".join(f"{t}:{i}" for t, i in records.missing_refs) or ""
        if unknown_blocks:
            detail += (
                (", " if detail else "") + "product blocks for unknown offerings " + ", ".join(sorted(unknown_blocks))
            )
        checks.append(
            GateCheck(
                name="references",
                passed=False,
                detail=detail,
                guidance="Only reference ids returned by tools or shown in the catalog cards.",
            )
        )
    else:
        checks.append(GateCheck(name="references", passed=True))

    # 2. amounts
    if sending:
        bad = [a for a in extract_amounts(text, records.quantity_numbers) if a.paise not in records.amounts_paise]
        if bad:
            known = ", ".join(format_inr(p) for p in sorted(records.amounts_paise)[:12]) or "none"
            checks.append(
                GateCheck(
                    name="amounts",
                    passed=False,
                    detail="not grounded: " + ", ".join(a.raw for a in bad),
                    guidance=f"State only prices from referenced records (known amounts: {known}); "
                    "add the record to referenced_records, or use a product block instead of typing prices.",
                )
            )
        else:
            checks.append(GateCheck(name="amounts", passed=True))

    # 3. discounts
    if sending:
        mentioned = [m.group(1) for m in _PCT.finditer(text)]
        mentioned += [
            g
            for m in _DISCOUNT_NEAR.finditer(text)
            if not _NEGATION.search(text[max(0, m.start() - 25) : m.end() + 25])  # "koi discount nahi hai"
            for g in m.groups()
            if g
        ]
        unbacked = [n for n in mentioned if not any(n.replace(",", "") in d for d in records.discount_texts)]
        checks.append(
            GateCheck(
                name="discounts",
                passed=not unbacked,
                detail=("unbacked discount: " + ", ".join(unbacked)) if unbacked else None,
                guidance="Never offer or invent discounts; if the customer asks, use action=ask_owner."
                if unbacked
                else None,
            )
        )

    # 4. links
    if sending:
        bad_urls = []
        for m in _URL.finditer(_EMAIL.sub(" ", text)):  # an email's domain is not a link
            url = m.group(0).rstrip(".,!?")
            host = _host(url)
            if host in PAYMENT_HOSTS:
                if not any(url.endswith(u.split("://", 1)[-1]) for u in records.allowed_urls):
                    bad_urls.append(url)
            elif host not in records.allowed_hosts:
                bad_urls.append(url)
        checks.append(
            GateCheck(
                name="links",
                passed=not bad_urls,
                detail=("links not allowed: " + ", ".join(bad_urls)) if bad_urls else None,
                guidance="Do not write URLs; use a payment_link block for payments." if bad_urls else None,
            )
        )

    # 5. contact details
    if sending:
        bad_contacts = [
            p.group(0) for p in _PHONE.finditer(text) if not _phone_allowed(p.group(0), records.allowed_phones)
        ]
        bad_contacts += [e.group(0) for e in _EMAIL.finditer(text) if e.group(0).lower() not in records.allowed_emails]
        checks.append(
            GateCheck(
                name="contacts",
                passed=not bad_contacts,
                detail=("contact details not allowed: " + ", ".join(bad_contacts)) if bad_contacts else None,
                guidance="Only share the business's own phone/email from the BUSINESS block." if bad_contacts else None,
            )
        )

    # 6. order status: never announce an order as confirmed while it is still a draft or awaiting payment
    if sending and records.open_order_status in ("draft", "awaiting_payment"):
        claim = next(
            (
                m
                for m in _CONFIRMED_CLAIM.finditer(text)
                if not _CONDITION_AFTER.match(text[m.end() : m.end() + 40])
                and not _CONDITION_BEFORE.search(text[max(0, m.start() - 40) : m.start()])
            ),
            None,
        )
        state = "awaiting payment" if records.open_order_status == "awaiting_payment" else "a draft"
        checks.append(
            GateCheck(
                name="order_status",
                passed=claim is None,
                detail=f"claims '{claim.group(0)}' but the order is {state}" if claim else None,
                guidance=(
                    f"The order is still {state}, not confirmed. Do not say it is confirmed or booked: read it back "
                    "and ask them to confirm"
                    + (" — it confirms once the payment is received." if state == "awaiting payment" else ".")
                )
                if claim
                else None,
            )
        )

    # 7. grounding
    if output.action == "send" and output.topic in GROUNDED_TOPICS and not records.valid_refs:
        checks.append(
            GateCheck(
                name="grounding",
                passed=False,
                detail=f"topic {output.topic} without references",
                guidance="Look the facts up with a tool and list them in referenced_records, "
                "or use action=ask_owner if the business has not told you.",
            )
        )
    else:
        checks.append(GateCheck(name="grounding", passed=True))

    # 8. tier rules (rewrite, not fail)
    rewritten: str | None = None
    if output.action == "send":
        if output.topic in HANDOFF_TOPICS or output.flags.angry:
            rewritten = "handoff"
        elif output.topic == "discount_negotiation" and not records.discount_texts:
            rewritten = "ask_owner"
        elif output.topic == "pricing" and records.price_on_request:
            rewritten = "ask_owner"
    if rewritten:
        checks.append(GateCheck(name="tier", passed=True, detail=f"send → {rewritten}"))

    passed = all(c.passed for c in checks)
    return GateResult(
        passed=passed,
        checks=checks,
        rewritten_action=rewritten if passed else None,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
