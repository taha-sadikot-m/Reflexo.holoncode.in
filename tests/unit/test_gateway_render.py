"""Rendering: blocks → IR with per-channel degradation, option maps, reply mapping, opt words (POC_03 §8, §9.2)."""

from __future__ import annotations

from uuid import UUID

import pytest

from reflexo.common.blocks import (
    ButtonsBlock,
    CarouselBlock,
    ListBlock,
    Option,
    OrderSummaryBlock,
    OrderSummaryView,
    PaymentLinkBlock,
    PaymentLinkView,
    ProductBlock,
    ProductView,
    TextBlock,
    VariantLine,
)
from reflexo.common.ids import new_id
from reflexo.gateway.adapters.base import IRCards, IRChoices, IRImage, IRLink, IRText
from reflexo.gateway.manifests import manifest_for
from reflexo.gateway.policy import detect_opt
from reflexo.gateway.render import Renderer, map_reply_to_option, similarity, split_text

BIZ = new_id()


class FakeSigner:
    def url_for(self, media_object_id: UUID) -> str:
        return f"https://reflexo.test/media/{media_object_id}?sig=x"


class FakeHydrator:
    link_status = "created"

    async def product(self, business_id: UUID, offering_id: UUID, variant_id: UUID | None) -> ProductView:
        return ProductView(
            offering_id=offering_id,
            name=f"Cake {str(offering_id)[-4:]}",
            description="Belgian chocolate",
            lines=[
                VariantLine(variant_id=None, name="500 g", price_display="₹550"),
                VariantLine(variant_id=None, name="1 kg", price_display="₹950"),
            ],
            image_url="https://cdn.test/cake.jpg",
        )

    async def payment_link(self, business_id: UUID, payment_link_id: UUID) -> PaymentLinkView:
        return PaymentLinkView(
            payment_link_id=payment_link_id,
            url="https://rzp.io/l/abc",
            amount_display="₹950",
            description="Order #12",
            status=self.link_status,
        )

    async def order_summary(self, business_id: UUID, order_id: UUID) -> OrderSummaryView:
        return OrderSummaryView(
            order_id=order_id, order_number="MB-0012", lines=["1 × Chocolate Truffle 1 kg: ₹950"], total_display="₹950"
        )


def renderer(provider: str, hydrator: FakeHydrator | None = None) -> Renderer:
    return Renderer(manifest_for(provider), hydrator or FakeHydrator(), FakeSigner())


def options(n: int) -> list[Option]:
    return [Option(id=f"opt{i}", title=f"Choice {i}") for i in range(1, n + 1)]


# ---------------------------------------------------------------- text splitting


def test_split_text_short_is_unchanged() -> None:
    assert split_text("hello", 100) == ["hello"]


def test_split_text_prefers_paragraph_then_sentence() -> None:
    para = "A" * 60 + "\n\n" + "B" * 60
    assert split_text(para, 100) == ["A" * 60, "B" * 60]
    sentences = ("This is sentence one. " * 6).strip()
    chunks = split_text(sentences, 50)
    assert all(len(c) <= 50 for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])


def test_split_text_hard_cut_without_spaces() -> None:
    chunks = split_text("x" * 250, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]


async def test_long_text_split_per_channel_limit() -> None:
    result = await renderer("meta_instagram").render(BIZ, [TextBlock(text="word " * 500)])
    assert len(result.parts) > 1
    assert all(isinstance(p, IRText) and len(p.text) <= 1000 for p in result.parts)


# ---------------------------------------------------------------- choice degradation


@pytest.mark.parametrize(
    ("provider", "n", "style"),
    [
        ("meta_whatsapp", 3, "buttons"),
        ("meta_whatsapp", 7, "list"),
        ("meta_pages", 3, "buttons"),
        ("meta_pages", 8, "quick_replies"),
        ("meta_instagram", 3, "quick_replies"),  # IG has no buttons
        ("telegram", 6, "buttons"),
        ("simulator", 10, "buttons"),
    ],
)
async def test_buttons_degrade_per_manifest(provider: str, n: int, style: str) -> None:
    result = await renderer(provider).render(BIZ, [ButtonsBlock(text="Pick one", options=options(n))])
    [part] = result.parts
    assert isinstance(part, IRChoices)
    assert part.style == style
    assert [o["n"] for o in result.options] == list(range(1, n + 1))


async def test_too_many_options_become_numbered_text() -> None:
    result = await renderer("meta_whatsapp").render(BIZ, [ListBlock(text="Flavours", options=options(14))])
    [part] = result.parts
    assert isinstance(part, IRText)
    assert "1) Choice 1" in part.text and "14) Choice 14" in part.text
    assert part.text.endswith("Reply with a number.")
    assert len(result.options) == 14


async def test_titles_truncated_but_full_title_kept_as_alias() -> None:
    long = Option(id="x", title="Chocolate truffle with extra ganache")
    result = await renderer("meta_whatsapp").render(BIZ, [ButtonsBlock(text="Pick", options=[long])])
    [part] = result.parts
    assert isinstance(part, IRChoices)
    assert len(part.options[0].title) <= 20 and part.options[0].title.endswith("…")
    assert "Chocolate truffle with extra ganache" in part.options[0].aliases


async def test_option_numbers_continue_across_blocks() -> None:
    result = await renderer("meta_whatsapp").render(
        BIZ,
        [ButtonsBlock(text="Size?", options=options(2)), ButtonsBlock(text="When?", options=options(2))],
    )
    assert [o["n"] for o in result.options] == [1, 2, 3, 4]


# ---------------------------------------------------------------- hydrated blocks


async def test_product_on_whatsapp_is_captioned_image_plus_cta() -> None:
    oid = new_id()
    result = await renderer("meta_whatsapp").render(
        BIZ, [ProductBlock(offering_id=oid, cta=[Option(id="order", title="Order")])]
    )
    image, choices = result.parts
    assert isinstance(image, IRImage) and image.caption is not None
    assert "*Cake" in image.caption and "1 kg: ₹950" in image.caption
    assert isinstance(choices, IRChoices) and choices.style == "buttons"


async def test_product_on_messenger_has_separate_caption() -> None:
    result = await renderer("meta_pages").render(BIZ, [ProductBlock(offering_id=new_id())])
    assert [type(p) for p in result.parts] == [IRImage, IRText]


async def test_carousel_native_on_messenger_degrades_on_whatsapp() -> None:
    items = [ProductBlock(offering_id=new_id()) for _ in range(4)]
    fb = await renderer("meta_pages").render(BIZ, [CarouselBlock(items=items)])
    wa = await renderer("meta_whatsapp").render(BIZ, [CarouselBlock(items=items)])
    assert isinstance(fb.parts[0], IRCards) and len(fb.parts[0].items) == 4
    assert fb.options[0]["option_id"].startswith("order:")
    assert isinstance(wa.parts[0], IRImage)
    assert isinstance(wa.parts[1], IRChoices) and wa.parts[1].style == "list"
    assert wa.options[0]["option_id"].startswith("product:")
    assert "₹550" in wa.parts[1].options[0].aliases[0]


async def test_payment_link_button_or_text() -> None:
    block = PaymentLinkBlock(payment_link_id=new_id())
    tg = await renderer("telegram").render(BIZ, [block])
    wa = await renderer("meta_whatsapp").render(BIZ, [block])
    assert isinstance(tg.parts[0], IRLink) and tg.parts[0].url == "https://rzp.io/l/abc"
    assert isinstance(wa.parts[0], IRText) and "https://rzp.io/l/abc" in wa.parts[0].text
    assert "https://rzp.io/l/abc" in wa.text_fallback


@pytest.mark.parametrize(("status", "shown"), [("paid", "✅ Paid"), ("expired", "has expired")])
async def test_a_used_or_dead_link_renders_its_state_not_the_link(
    status: str, shown: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 13 Oct: paid a second after creation, the reply's link card failed to render and the turn fell back."""
    monkeypatch.setattr(FakeHydrator, "link_status", status)
    wa = await renderer("meta_whatsapp").render(BIZ, [PaymentLinkBlock(payment_link_id=new_id())])
    [part] = wa.parts
    assert isinstance(part, IRText) and shown in part.text and "rzp.io" not in part.text


async def test_order_summary_text() -> None:
    result = await renderer("simulator").render(BIZ, [OrderSummaryBlock(order_id=new_id())])
    [part] = result.parts
    assert isinstance(part, IRText) and "MB-0012" in part.text and "Total: ₹950" in part.text


async def test_hydrated_block_without_hydrator_fails_loudly() -> None:
    r = Renderer(manifest_for("meta_whatsapp"), None, FakeSigner())
    with pytest.raises(RuntimeError, match="BlockHydrator"):
        await r.render(BIZ, [ProductBlock(offering_id=new_id())])


# ---------------------------------------------------------------- reply → option mapping

OPTS = [
    {"n": 1, "option_id": "half", "title": "Half kg", "aliases": []},
    {"n": 2, "option_id": "one", "title": "One kg", "aliases": ["1 kg cake"]},
    {"n": 3, "option_id": "two", "title": "Two kg", "aliases": []},
]


@pytest.mark.parametrize(
    ("reply", "option_id"),
    [
        ("2", "one"),
        (" 3. ", "two"),
        ("२", "one"),  # Devanagari digit
        ("do", "one"),  # Hinglish
        ("दो", "one"),  # Hindi
        ("pehla", "half"),
        ("second", "one"),
        ("Half kg", "half"),
        ("half kg please", "half"),
        ("1 kg cake", "one"),  # alias
    ],
)
def test_map_reply_to_option(reply: str, option_id: str) -> None:
    hit = map_reply_to_option(reply, OPTS)
    assert hit is not None and hit["option_id"] == option_id


@pytest.mark.parametrize("reply", ["9", "what time do you close?", "", "chocolate"])
def test_map_reply_no_match(reply: str) -> None:
    assert map_reply_to_option(reply, OPTS) is None


def test_similarity_bounds() -> None:
    assert similarity("Half kg", "half kg") == 1.0
    assert similarity("abc", "xyz") == 0.0


# ---------------------------------------------------------------- opt-out / opt-in words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STOP", "out"),
        ("stop.", "out"),
        ("Band karo", "out"),
        ("बंद करो", "out"),
        ("start", "in"),
        ("Shuru karo!", "in"),
        ("please stop sending the cake photo", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_opt(text: str | None, expected: str | None) -> None:
    assert detect_opt(text) == expected


def test_ambiguous_phrase_match_is_left_to_the_agent() -> None:
    assert map_reply_to_option("not half kg, one kg", OPTS) is None


def test_text_normalize_keeps_devanagari_marks() -> None:
    from reflexo.common.text import normalize

    assert normalize("बंद करो!!") == "बंद करो"
    assert normalize("  Half-KG,  please ") == "half kg please"


def test_long_sentence_mentioning_a_title_is_not_a_button_press() -> None:
    """Live: "Do not take more than 8 orders a day, confirm COD orders…" pressed the owner's [✅ Confirm]."""
    opts = [
        {"n": 1, "option_id": "apv:x:a", "title": "✅ Confirm", "aliases": []},
        {"n": 2, "option_id": "apv:x:r", "title": "✖ Cancel", "aliases": []},
    ]
    long = "Do not take more than 8 order in day, confirm COD orders and send reminder to customer before delivery"
    assert map_reply_to_option(long, opts) is None
    assert map_reply_to_option("ok confirm it", opts)["option_id"] == "apv:x:a"  # type: ignore[index]
    assert map_reply_to_option("cancel please", opts)["option_id"] == "apv:x:r"  # type: ignore[index]
