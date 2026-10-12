"""Content blocks → IR parts, with hydration, capability-driven degradation and option maps (POC_03 §8.3–8.4)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from reflexo.common.blocks import (
    ButtonsBlock,
    CarouselBlock,
    ContentBlock,
    FileBlock,
    ImageBlock,
    ListBlock,
    Option,
    OrderSummaryBlock,
    PaymentLinkBlock,
    ProductBlock,
    ProductView,
    TextBlock,
)
from reflexo.common.contracts import BlockHydrator
from reflexo.common.text import normalize
from reflexo.gateway.adapters.base import IRCards, IRChoices, IRFile, IRImage, IRLink, IRPart, IRText, Manifest


class MediaUrlSigner(Protocol):
    def url_for(self, media_object_id: UUID) -> str: ...


@dataclass
class RenderResult:
    parts: list[IRPart]
    text_fallback: str  # plain text of everything (stored in messages.content_text)
    options: list[dict[str, Any]] = field(default_factory=list)  # option map entries [{n, option_id, title, aliases}]


def split_text(text: str, limit: int) -> list[str]:
    """Split at paragraph, then sentence, then word boundaries so each chunk fits `limit`."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit * 0.5:
            cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "), window.rfind("। "))
            cut = cut + 1 if cut > 0 else -1
        if cut < limit * 0.5:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def product_caption(view: ProductView) -> str:
    lines = [f"*{view.name}*"]
    if view.description:
        lines.append(view.description)
    if view.price_on_request:
        lines.append("Price on request")
    for line in view.lines:
        lines.append(f"• {line.name}: {line.price_display}")
    return "\n".join(lines)


class Renderer:
    def __init__(self, manifest: Manifest, hydrator: BlockHydrator | None, signer: MediaUrlSigner) -> None:
        self.m = manifest
        self.hydrator = hydrator
        self.signer = signer
        self._options: list[dict[str, Any]] = []
        self._text: list[str] = []

    def _need_hydrator(self) -> BlockHydrator:
        if self.hydrator is None:
            raise RuntimeError("BlockHydrator port not provided; cannot render product/payment/order blocks")
        return self.hydrator

    def _numbered(self, text: str, options: list[Option]) -> IRText:
        start = len(self._options)
        lines = [text]
        for i, opt in enumerate(options, start=start + 1):
            self._options.append({"n": i, "option_id": opt.id, "title": opt.title, "aliases": opt.aliases})
            lines.append(f"{i}) {opt.title}")
        lines.append("Reply with a number.")
        return IRText("\n".join(lines))

    def _register(self, options: list[Option]) -> None:
        start = len(self._options)
        for i, opt in enumerate(options, start=start + 1):
            self._options.append({"n": i, "option_id": opt.id, "title": opt.title, "aliases": opt.aliases})

    def choices(self, text: str, options: list[Option], prefer: str, button_label: str = "Options") -> list[IRPart]:
        m = self.m
        if prefer == "buttons" and len(options) <= m.max_buttons:
            self._register(options)
            return [IRChoices(text, self._fit(options, m.button_title_max), "buttons", button_label)]
        if m.list_supported and len(options) <= m.list_max_rows:
            self._register(options)
            return [IRChoices(text, self._fit(options, m.row_title_max), "list", button_label)]
        if len(options) <= m.max_buttons:
            self._register(options)
            return [IRChoices(text, self._fit(options, m.button_title_max), "buttons", button_label)]
        if m.quick_replies_max and len(options) <= m.quick_replies_max:
            self._register(options)
            return [IRChoices(text, self._fit(options, m.button_title_max), "quick_replies", button_label)]
        return [self._numbered(text, options)]

    @staticmethod
    def _fit(options: list[Option], limit: int) -> list[Option]:
        return [Option(id=o.id, title=_truncate(o.title, limit), aliases=[*o.aliases, o.title]) for o in options]

    async def block(self, business_id: UUID, b: ContentBlock) -> list[IRPart]:
        if isinstance(b, TextBlock):
            self._text.append(b.text)
            return [IRText(chunk) for chunk in split_text(b.text, self.m.max_text)]
        if isinstance(b, ImageBlock):
            url = b.url or self.signer.url_for(b.media_object_id)  # type: ignore[arg-type]
            if b.caption:
                self._text.append(b.caption)
            return [
                IRImage(url, _truncate(b.caption, self.m.caption_max) if b.caption and self.m.caption_max else None)
            ]
        if isinstance(b, FileBlock):
            if b.caption:
                self._text.append(b.caption)
            return [IRFile(self.signer.url_for(b.media_object_id), b.filename, b.caption)]
        if isinstance(b, ButtonsBlock):
            self._text.append(b.text)
            return self.choices(b.text, b.options, "buttons")
        if isinstance(b, ListBlock):
            self._text.append(b.text)
            return self.choices(b.text, b.options, "list", b.button_label)
        if isinstance(b, ProductBlock):
            view = await self._need_hydrator().product(business_id, b.offering_id, b.variant_id)
            return self._product(view, b.cta)
        if isinstance(b, CarouselBlock):
            views = [await self._need_hydrator().product(business_id, i.offering_id, i.variant_id) for i in b.items]
            return self._carousel(views)
        if isinstance(b, PaymentLinkBlock):
            link = await self._need_hydrator().payment_link(business_id, b.payment_link_id)
            # the status is stated by the card itself, not left to the model's sentence (live: "your order is
            # confirmed!" next to a link that had not been paid)
            text = f"{link.description}\nAmount: {link.amount_display}\n⏳ Confirms automatically once paid"
            self._text.append(f"{text}\n{link.url}")
            if self.m.link_button:
                return [IRLink(text, link.url, "Pay now")]
            return [IRText(f"{text}\nPay securely here: {link.url}")]
        if isinstance(b, OrderSummaryBlock):
            o = await self._need_hydrator().order_summary(business_id, b.order_id)
            lines = [f"🧾 Order {o.order_number}", *o.lines, f"Total: {o.total_display}"]
            if o.delivery:
                lines.append(f"Delivery: {o.delivery}")
            if o.payment:
                lines.append(f"Payment: {o.payment}")
            text = "\n".join(lines)
            self._text.append(text)
            return [IRText(text)]
        raise TypeError(f"unsupported block {type(b).__name__}")

    def _product(self, view: ProductView, cta: list[Option]) -> list[IRPart]:
        caption = product_caption(view)
        self._text.append(caption)
        parts: list[IRPart] = []
        if view.image_url and self.m.caption_max:
            parts.append(IRImage(view.image_url, _truncate(caption, self.m.caption_max)))
        elif view.image_url:
            parts += [IRImage(view.image_url), IRText(caption)]
        else:
            parts.append(IRText(caption))
        if cta:
            parts += self.choices("What would you like to do?", cta, "buttons")
        return parts

    def _carousel(self, views: list[ProductView]) -> list[IRPart]:
        if self.m.carousel_supported:
            for v in views:
                self._text.append(product_caption(v))
            self._register([Option(id=f"order:{v.offering_id}", title=v.name) for v in views])
            return [IRCards(views[: self.m.carousel_max])]
        parts: list[IRPart] = []
        first = views[0]
        if first.image_url:
            parts.append(IRImage(first.image_url))
        options = [
            Option(id=f"product:{v.offering_id}", title=f"{v.name} — {v.lines[0].price_display}" if v.lines else v.name)
            for v in views
        ]
        self._text.append("\n".join(o.title for o in options))
        return parts + self.choices("Here are some options:", options, "list", "View")

    async def render(self, business_id: UUID, blocks: list[ContentBlock]) -> RenderResult:
        parts: list[IRPart] = []
        for b in blocks:
            parts += await self.block(business_id, b)
        return RenderResult(parts=parts, text_fallback="\n\n".join(self._text), options=self._options)


# ---------------------------------------------------------------- inbound option mapping

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "ek": 1,
    "do": 2,
    "teen": 3,
    "tin": 3,
    "char": 4,
    "chaar": 4,
    "paanch": 5,
    "panch": 5,
    "chhe": 6,
    "che": 6,
    "saat": 7,
    "aath": 8,
    "nau": 9,
    "das": 10,
    "pehla": 1,
    "pehli": 1,
    "pahla": 1,
    "dusra": 2,
    "doosra": 2,
    "dusri": 2,
    "teesra": 3,
    "tisra": 3,
    "teesri": 3,
    "एक": 1,
    "दो": 2,
    "तीन": 3,
    "चार": 4,
    "पांच": 5,
    "पाँच": 5,
    "छह": 6,
    "सात": 7,
    "आठ": 8,
    "नौ": 9,
    "दस": 10,
}


def _norm(s: str) -> str:
    return normalize(s)


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(_norm(a)), _trigrams(_norm(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def map_reply_to_option(text: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a free-text reply to an offered option: digits, number words (en/hi/hi-Latn), or a fuzzy title match."""
    if not options or not text:
        return None
    t = _norm(text)
    digits = unicodedata.normalize("NFKC", t)
    devanagari_digits = str.maketrans("०१२३४५६७८९", "0123456789")
    digits = digits.translate(devanagari_digits)
    n: int | None = None
    if re.fullmatch(r"\d{1,2}", digits):
        n = int(digits)
    elif t in _NUMBER_WORDS:
        n = _NUMBER_WORDS[t]
    if n is not None:
        return next((o for o in options if o["n"] == n), None)
    # a short reply that contains exactly one option's title as a whole phrase ("half kg please"); a long sentence
    # that merely mentions a title ("… confirm COD orders and send reminders") is not a button press
    padded = f" {t} "
    contained = [
        o
        for o in options
        if any(
            len(c) >= 3 and f" {c} " in padded and len(t.split()) <= len(c.split()) + 3
            for c in (_norm(x) for x in [o["title"], *o.get("aliases", [])])
        )
    ]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        return None  # ambiguous ("not half kg, one kg"): let the agent read it
    best, best_score = None, 0.0
    for o in options:
        for candidate in [o["title"], *o.get("aliases", [])]:
            score = similarity(t, candidate)
            if score > best_score:
                best, best_score = o, score
    return best if best_score >= 0.6 else None
