"""Live Gemini smoke test (opt-in: `uv run pytest --live tests/live`). Needs REFLEXO_GOOGLE_API_KEY in .env.

Verifies the real model IDs, tool calling with thought signatures, JSON mode and 768-d embeddings.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from reflexo.agent.model_gateway.config import load_models_config
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.model_gateway.providers import GeminiProvider
from reflexo.common.config import Settings
from reflexo.common.llm import CallCtx, ChatMessage, ToolSpec

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def gw() -> ModelGateway:
    # read the developer's real .env, not the test overrides
    settings = Settings(_env_file=".env")  # type: ignore[call-arg]
    key = settings.google_api_key.get_secret_value()
    if not key:
        pytest.skip("REFLEXO_GOOGLE_API_KEY not set")
    return ModelGateway(GeminiProvider(key), load_models_config(settings))


def ctx() -> CallCtx:
    return CallCtx(business_id=None, purpose="live-test")


async def test_chat_hinglish(gw: ModelGateway) -> None:
    c = ctx()
    r = await gw.chat(
        "customer_desk",
        [ChatMessage.system("Reply in one short Hinglish sentence."), ChatMessage.user("Cake kab tak deliver hoga?")],
        ctx=c,
    )
    assert r.text.strip() and r.input_tokens > 0
    print(f"\n{r.model} ttft={r.ttft_ms}ms total={r.latency_ms}ms: {r.text!r}")


async def test_tool_call_round_trip(gw: ModelGateway) -> None:
    spec = ToolSpec(
        name="catalog_search",
        description="Search the bakery catalogue",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    msgs = [
        ChatMessage.system("Always use catalog_search before answering product questions."),
        ChatMessage.user("Chocolate truffle 1 kg kitne ka hai?"),
    ]
    first = await gw.chat("customer_desk", msgs, [spec], ctx=ctx())
    assert first.tool_calls and first.tool_calls[0].name == "catalog_search"
    msgs += [
        ChatMessage.assistant(first.text or None, first.tool_calls),
        ChatMessage.tool(first.tool_calls[0], '{"cards": [{"name": "Chocolate Truffle Cake", "1 kg": "₹950"}]}'),
    ]
    second = await gw.chat("customer_desk", msgs, [spec], ctx=ctx())
    assert "950" in second.text


class Scope(BaseModel):
    scope: str
    confidence: float


async def test_structured(gw: ModelGateway) -> None:
    out = await gw.structured(
        "classifier",
        [
            ChatMessage.user(
                'Owner answered a customer question with "isko 10% de do". Is this for this customer only '
                "(customer) or "
                'everyone (global)? Return JSON {"scope": ..., "confidence": 0..1}.'
            )
        ],
        Scope,
        ctx=ctx(),
    )
    assert out.scope in ("customer", "global")


async def test_embeddings_768(gw: ModelGateway) -> None:
    [a, b, c] = await gw.embed(["chocolate truffle cake", "चॉकलेट केक", "delivery charges"], task="document")
    assert len(a) == 768

    def cos(x: list[float], y: list[float]) -> float:
        return sum(i * j for i, j in zip(x, y, strict=True))

    assert cos(a, b) > cos(a, c)  # Hindi "chocolate cake" is closer to the cake than to delivery
