"""Prices, cost arithmetic and GenAI spans (WP-01; BETA_12 §6, §7)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import reflexo.agent.api as agent
from reflexo.agent.model_gateway.config import EmbeddingConfig, ModelsConfig, RoleConfig, load_models_config
from reflexo.common.config import get_settings
from reflexo.common.costs import PriceMissing, Usage, cost_usd, load_price_book, micro_inr, price_book
from reflexo.common.llm import CallCtx, ChatMessage, ToolCall
from reflexo.common.tracing import GENAI
from reflexo.voice.metering import stt_price_key

PRICES = Path(__file__).resolve().parents[2] / "config" / "model_prices.yaml"
RATE = Decimal("95")


def test_worked_budget_per_call_matches_beta_12() -> None:
    """BETA_12 §4.5: 3,000 uncached + 3,000 cached input and 400 output tokens at ₹95 per dollar."""
    book = load_price_book(PRICES)
    usage = Usage(input_tokens=6000, cached_input_tokens=3000, output_tokens=400)
    lite = micro_inr(cost_usd(book.price("gemini", "gemini-2.5-flash-lite"), usage), RATE)
    flash = micro_inr(cost_usd(book.price("gemini", "gemini-2.5-flash"), usage), RATE)
    assert lite == 46_550  # ₹0.047 a call
    assert flash == 189_050  # ₹0.19 a call


def test_audio_and_character_items() -> None:
    book = load_price_book(PRICES)
    stt = cost_usd(book.price("deepgram", "nova-3-streaming-multi"), Usage(audio_seconds=Decimal(90)))
    tts = cost_usd(book.price("deepgram", "aura-2-asteria-en"), Usage(characters=2000))  # a voice of aura-2
    session = cost_usd(book.price("livekit", "agent_session"), Usage(audio_seconds=Decimal(60)))
    assert (stt, tts, session) == (Decimal("0.0087"), Decimal("0.060"), Decimal("0.01"))


def test_cached_input_without_a_cached_price_is_billed_as_input() -> None:
    book = load_price_book(PRICES)
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000)
    assert cost_usd(book.price("gemini", "gemini-embedding-001"), usage) == Decimal("0.15")


def test_unknown_model_has_no_price() -> None:
    with pytest.raises(PriceMissing):
        load_price_book(PRICES).price("gemini", "gemini-9-ultra")


def test_every_model_in_use_has_a_price() -> None:
    """A model that is configured but not priced would be metered at no cost: the price file must change first."""
    s = get_settings()
    book = price_book()
    cfg = load_models_config()
    for role in cfg.roles.values():
        for model in (role.primary, role.fallback):
            if model:
                book.price("gemini", model)
    book.price("gemini", cfg.embedding.model)
    book.price("gemini", s.voice_model)
    book.price("deepgram", stt_price_key())
    book.price("deepgram", s.tts_model)
    book.price("livekit", "agent_session")


@pytest.fixture
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.mark.check("CHK-PC-2")
async def test_spans_carry_genai_attributes_and_no_content(spans: InMemorySpanExporter) -> None:
    fake = agent.FakeProvider()
    secret = "my address is 12 Lake Road, call me on 98111 22233"
    fake.reply(
        "gemini-2.5-flash",
        agent.FakeReply(
            tool_calls=[ToolCall(id="1", name="reply", args={"blocks": [{"type": "text", "text": secret}]})],
            input_tokens=1200,
            cached_input_tokens=800,
            output_tokens=90,
        ),
    )
    cfg = ModelsConfig(
        roles={"customer_desk": RoleConfig(primary="gemini-2.5-flash")}, embedding=EmbeddingConfig(model="e")
    )
    gw = agent.ModelGateway(fake, cfg)
    ctx = CallCtx(business_id=None, purpose="customer_turn", prompt_version="customer@abc")
    await gw.chat("customer_desk", [ChatMessage.system("rules"), ChatMessage.user(secret)], ctx=ctx)

    [span] = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    a = dict(span.attributes or {})
    assert span.name == "chat gemini-2.5-flash"
    assert a[GENAI.OPERATION] == "chat" and a[GENAI.PROVIDER] == "fake"
    assert a[GENAI.REQUEST_MODEL] == a[GENAI.RESPONSE_MODEL] == "gemini-2.5-flash"
    assert (a[GENAI.INPUT_TOKENS], a[GENAI.CACHED_INPUT_TOKENS], a[GENAI.OUTPUT_TOKENS]) == (1200, 800, 90)
    assert a[GENAI.PURPOSE] == "customer_turn" and a[GENAI.FEATURE] == "chat"
    assert a[GENAI.PROMPT_VERSION] == "customer@abc" and GENAI.COST_MICRO_INR in a
    for value in a.values():  # message content never reaches a span (BETA_10 §12)
        assert "Lake Road" not in str(value) and "98111" not in str(value) and "rules" != str(value)
