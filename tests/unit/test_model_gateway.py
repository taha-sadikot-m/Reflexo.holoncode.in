"""Model gateway: hedging, failover, timeouts, chaos, structured output, embeddings, LangChain adapter, Gemini
request mapping (POC_04 §2). Uses the scripted FakeProvider; timings are tens of milliseconds."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from reflexo.agent.model_gateway import gateway as gateway_mod
from reflexo.agent.model_gateway.config import EmbeddingConfig, ModelsConfig, RoleConfig, load_models_config
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.model_gateway.langchain import GatewayChatModel, to_gateway_messages
from reflexo.agent.model_gateway.providers import Attempt, FakeProvider, FakeReply, GeminiProvider
from reflexo.common.llm import CallCtx, ChatMessage, ModelError, ToolCall, ToolSpec, current_call_ctx


def config(**roles: RoleConfig) -> ModelsConfig:
    base = {
        "chat": RoleConfig(primary="p", fallback="f", hedge_after_ms=60, timeout_ms=600),
        "nohedge": RoleConfig(primary="p", fallback="f", hedge_after_ms=None, timeout_ms=600),
        "solo": RoleConfig(primary="p", timeout_ms=200),
    }
    return ModelsConfig(roles=base | roles, embedding=EmbeddingConfig(model="emb", dimensions=8, batch_size=2))


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def gw(fake: FakeProvider) -> ModelGateway:
    return ModelGateway(fake, config())


def ctx() -> CallCtx:
    return CallCtx(business_id=None, purpose="test")


MSG = [ChatMessage.system("be brief"), ChatMessage.user("hi")]


# ---------------------------------------------------------------- hedging and failover


async def test_fast_primary_wins_without_hedge(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", "primary")
    c = ctx()
    result = await gw.chat("chat", MSG, ctx=c)
    assert (result.text, result.model, result.hedged) == ("primary", "p", False)
    assert [a.model for a in fake.calls] == ["p"]
    assert (c.stats.model_calls, c.stats.model_used, c.stats.input_tokens) == (1, "p", 10)


async def test_slow_first_token_hedges_and_fallback_wins(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(text="primary", first_token_ms=400, total_ms=400))
    fake.reply("f", FakeReply(text="fallback", first_token_ms=10, total_ms=20))
    c = ctx()
    result = await gw.chat("chat", MSG, ctx=c)
    assert (result.text, result.model, result.hedged) == ("fallback", "f", True)
    assert fake.cancelled == ["p"]  # the loser is cancelled, not left running
    assert c.stats.hedged and c.stats.model_used == "f"


async def test_hedged_primary_can_still_win(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(text="primary", first_token_ms=100, total_ms=110))
    fake.reply("f", FakeReply(text="fallback", first_token_ms=300, total_ms=300))
    result = await gw.chat("chat", MSG, ctx=ctx())
    assert (result.model, result.hedged) == ("p", True)
    assert fake.cancelled == ["f"]


async def test_primary_error_fails_over_immediately(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(error=ModelError("RATE_LIMITED", "429"), first_token_ms=5))
    fake.reply("f", "fallback")
    result = await gw.chat("chat", MSG, ctx=ctx())
    assert (result.model, result.hedged) == ("f", False)


async def test_non_retryable_error_is_raised(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(error=ModelError("CREDENTIAL_INVALID", "bad key", retryable=False)))
    with pytest.raises(ModelError) as err:
        await gw.chat("chat", MSG, ctx=ctx())
    assert err.value.code == "CREDENTIAL_INVALID"
    assert [a.model for a in fake.calls] == ["p"]


async def test_both_failing_raises_last_error(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(error=ModelError("PROVIDER_UNAVAILABLE", "500")))
    fake.reply("f", FakeReply(error=ModelError("RATE_LIMITED", "429")))
    with pytest.raises(ModelError) as err:
        await gw.chat("chat", MSG, ctx=ctx())
    assert err.value.code == "RATE_LIMITED"


async def test_timeout_cancels_everything(fake: FakeProvider) -> None:
    gw = ModelGateway(fake, config(chat=RoleConfig(primary="p", fallback="f", hedge_after_ms=30, timeout_ms=150)))
    fake.reply("p", FakeReply(text="late", first_token_ms=1000))
    fake.reply("f", FakeReply(text="late", first_token_ms=1000))
    with pytest.raises(ModelError) as err:
        await gw.chat("chat", MSG, ctx=ctx())
    assert err.value.code == "TIMEOUT"
    assert sorted(fake.cancelled) == ["f", "p"]


async def test_role_without_hedge_waits_for_primary(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", FakeReply(text="slow but fine", first_token_ms=150, total_ms=150))
    result = await gw.chat("nohedge", MSG, ctx=ctx())
    assert (result.model, result.hedged) == ("p", False)
    assert [a.model for a in fake.calls] == ["p"]


async def test_chaos_delay_triggers_fallback(
    gw: ModelGateway, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow() -> float:
        return 0.3

    monkeypatch.setattr(gateway_mod, "_chaos_delay_s", slow)
    fake.reply("p", "primary")
    fake.reply("f", "fallback")
    result = await gw.chat("chat", MSG, ctx=ctx())
    assert (result.model, result.hedged) == ("f", True)
    assert fake.calls[0].pre_delay_s == 0.3 and fake.calls[1].pre_delay_s == 0  # only the primary is delayed


async def test_unknown_role(gw: ModelGateway) -> None:
    with pytest.raises(KeyError, match="unknown model role"):
        await gw.chat("nope", MSG, ctx=ctx())


# ---------------------------------------------------------------- tools


async def test_tool_calls_returned(gw: ModelGateway, fake: FakeProvider) -> None:
    call = ToolCall(id="c1", name="catalog_search", args={"query": "truffle"})
    fake.reply("p", FakeReply(tool_calls=[call]))
    spec = ToolSpec(name="catalog_search", description="search", parameters={"type": "object", "properties": {}})
    result = await gw.chat("chat", MSG, [spec], ctx=ctx())
    assert result.tool_calls == [call]
    assert fake.calls[0].tools == [spec]


# ---------------------------------------------------------------- structured output


class Scope(BaseModel):
    scope: str
    confidence: float


async def test_structured_valid(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", '{"scope": "global", "confidence": 0.9}')
    out = await gw.structured("chat", MSG, Scope, ctx=ctx())
    assert out == Scope(scope="global", confidence=0.9)
    assert fake.calls[0].response_schema is not None and "scope" in fake.calls[0].response_schema["properties"]


async def test_structured_invalid_primary_lets_fallback_win(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", '{"scope": "global"}')  # missing confidence
    fake.reply("f", '{"scope": "customer", "confidence": 0.7}')
    assert (await gw.structured("chat", MSG, Scope, ctx=ctx())).scope == "customer"


async def test_structured_repair_round(fake: FakeProvider) -> None:
    gw = ModelGateway(fake, config())
    fake.queue("p", '{"scope": 1}', '{"scope": "temporary", "confidence": 0.6}')
    out = await gw.structured("solo", MSG, Scope, ctx=ctx())
    assert out.scope == "temporary"
    repair_prompt = fake.calls[-1].messages[-1].content or ""
    assert "not valid JSON" in repair_prompt


async def test_structured_gives_up_after_repair(gw: ModelGateway, fake: FakeProvider) -> None:
    fake.reply("p", '{"nope": true}')
    with pytest.raises(ModelError) as err:
        await gw.structured("solo", MSG, Scope, ctx=ctx())
    assert err.value.code == "INVALID_OUTPUT"


# ---------------------------------------------------------------- embeddings


async def test_embeddings_batched_and_normalized(gw: ModelGateway, fake: FakeProvider) -> None:
    calls: list[int] = []
    original = fake.embed

    async def counting(model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]:
        calls.append(len(texts))
        return await original(model, texts, task=task, dimensions=dimensions)

    fake.embed = counting  # type: ignore[method-assign]
    vectors = await gw.embed(["a b", "c", "d e f", "g", "h"], task="document")
    assert calls == [2, 2, 1]
    assert len(vectors) == 5 and all(len(v) == 8 for v in vectors)
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-6 for v in vectors)


async def test_embedding_retries_once(gw: ModelGateway, fake: FakeProvider) -> None:
    attempts = 0

    async def flaky(model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelError("PROVIDER_UNAVAILABLE", "503")
        return [[1.0] * dimensions for _ in texts]

    fake.embed = flaky  # type: ignore[method-assign]
    assert len(await gw.embed(["x"], task="query")) == 1 and attempts == 2


# ---------------------------------------------------------------- LangChain adapter


@tool
def catalog_search(query: str, max_price_paise: int | None = None) -> str:
    """Search the catalogue."""
    return query


async def test_langchain_model_with_tools(gw: ModelGateway, fake: FakeProvider) -> None:
    call = ToolCall(
        id="c9", name="catalog_search", args={"query": "truffle"}, provider_meta={"thought_signature": "c2ln"}
    )
    fake.reply("p", FakeReply(tool_calls=[call]))
    model = GatewayChatModel(gateway=gw, role="chat").bind_tools([catalog_search])
    c = ctx()
    token = current_call_ctx.set(c)
    try:
        msg = await model.ainvoke([SystemMessage("rules"), HumanMessage("truffle price?")])
    finally:
        current_call_ctx.reset(token)
    assert isinstance(msg, AIMessage)
    assert msg.tool_calls[0]["name"] == "catalog_search" and msg.tool_calls[0]["args"] == {"query": "truffle"}
    assert msg.response_metadata["model_name"] == "p"
    assert c.stats.model_calls == 1  # accounted to the run set in the context variable
    [spec] = fake.calls[0].tools or []
    assert spec.name == "catalog_search" and "query" in spec.parameters["properties"]

    # the thought signature survives the LangChain round trip back into the gateway
    history = to_gateway_messages([HumanMessage("q"), msg, ToolMessage(content='{"cards": []}', tool_call_id="c9")])
    assert history[1].tool_calls[0].provider_meta == {"thought_signature": "c2ln"}
    assert (history[2].role, history[2].name, history[2].tool_call_id) == ("tool", "catalog_search", "c9")


# ---------------------------------------------------------------- config


def test_models_yaml_interpolates_settings() -> None:
    cfg = load_models_config()
    from reflexo.common.config import get_settings

    s = get_settings()
    assert cfg.role("customer_desk").primary == s.model_primary
    assert cfg.role("builder").primary == s.model_fallback  # builder prefers the stronger/slower model
    assert cfg.role("customer_desk").thinking_budget == 0
    assert cfg.embedding.model == s.embedding_model and cfg.embedding.dimensions == 768


def test_same_primary_and_fallback_disables_hedging(tmp_path: Any) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(
        "roles:\n  r: {primary: a, fallback: a, hedge_after_ms: 10}\nembedding: {model: e}\n", encoding="utf-8"
    )
    assert load_models_config(path=path).role("r").fallback is None


# ---------------------------------------------------------------- Gemini request mapping (no network)


def test_gemini_contents_and_config() -> None:
    p = GeminiProvider("test-key")
    call = ToolCall(id="c1", name="catalog_search", args={"q": "x"}, provider_meta={"thought_signature": "c2ln"})
    call2 = ToolCall(id="c2", name="delivery_quote", args={"area": "Kothrud"})
    messages = [
        ChatMessage.system("rules A"),
        ChatMessage.system("rules B"),
        ChatMessage.user("hi"),
        ChatMessage.assistant(None, [call, call2]),
        ChatMessage.tool(call, '{"cards": [1]}'),
        ChatMessage.tool(call2, "not json"),
    ]
    system, contents = p._contents(messages)
    assert system == "rules A\n\nrules B"
    assert [c.role for c in contents] == ["user", "model", "user"]
    fc_part = contents[1].parts[0]
    assert fc_part.function_call.name == "catalog_search" and fc_part.thought_signature == base64.b64decode("c2ln")
    responses = [part.function_response for part in contents[2].parts]  # parallel results grouped in one turn
    assert [r.name for r in responses] == ["catalog_search", "delivery_quote"]
    assert responses[0].response == {"cards": [1]} and responses[1].response == {"result": "not json"}

    spec = ToolSpec(name="catalog_search", description="d", parameters={"type": "object", "properties": {}})
    attempt = Attempt(
        "gemini-x", messages, [spec], RoleConfig(primary="gemini-x", thinking_budget=0), response_schema=None
    )
    cfg = p._config(attempt, system)
    assert cfg.thinking_config.thinking_budget == 0
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tools[0].function_declarations[0].name == "catalog_search"


def test_gemini_error_mapping() -> None:
    from google.genai import errors

    p = GeminiProvider("test-key")
    assert p._map_error(errors.APIError(429, {"error": {"message": "quota"}}), "m").code == "RATE_LIMITED"
    assert p._map_error(errors.APIError(503, {"error": {"message": "x"}}), "m").code == "PROVIDER_UNAVAILABLE"
    denied = p._map_error(errors.APIError(403, {"error": {"message": "x"}}), "m")
    assert denied.code == "CREDENTIAL_INVALID" and not denied.retryable
    assert p._map_error(TimeoutError(), "m").code == "PROVIDER_UNAVAILABLE"


def test_gemini_requires_key() -> None:
    with pytest.raises(ModelError) as err:
        GeminiProvider("")
    assert err.value.code == "CREDENTIAL_INVALID"
