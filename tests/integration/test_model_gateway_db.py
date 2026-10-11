"""Model gateway with the database: the chaos flag from `runtime_flags`, `agent_runs` accounting, X-ray events,
and the embedder wired into knowledge search (WP-05 done-when; POC_04 §2.3, §12)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.knowledge.api as knowledge
from reflexo.common.contracts import EmbedderPort, ModelGatewayPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.flags import clear_caches
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx, ChatMessage
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api

pytestmark = pytest.mark.integration


@pytest.fixture
def fake() -> agent.FakeProvider:
    return agent.FakeProvider()


@pytest.fixture
def registry(fake: agent.FakeProvider) -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    agent.register(reg, provider=fake)
    knowledge.register(reg)
    return reg


async def _set_flag(db: Database, value: str | None) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        if value is None:
            await s.execute(text("DELETE FROM runtime_flags WHERE key = 'slow_model'"))
        else:
            await s.execute(
                text("""INSERT INTO runtime_flags (key, value) VALUES ('slow_model', CAST(:v AS jsonb))
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""),
                {"v": value},
            )
    clear_caches()


@pytest.fixture
async def slow_model_flag(db: Database) -> AsyncIterator[None]:
    await _set_flag(db, json.dumps({"delay_ms": 4000}))
    yield
    await _set_flag(db, None)


async def make_business(db: Database) -> object:
    bid = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO businesses (id, name, slug, status) VALUES (:id, 'M', :s, 'active')"),
            {"id": bid, "s": f"m-{bid}"},
        )
    return bid


async def test_chaos_slow_model_falls_back_and_run_records_model(
    registry: Registry, db: Database, fake: agent.FakeProvider, slow_model_flag: None
) -> None:
    cfg = agent.load_models_config().role("customer_desk")
    fake.reply(cfg.primary, "primary answer")
    fake.reply(cfg.fallback or "", "fallback answer")
    bid = await make_business(db)
    ctx = CallCtx(business_id=bid, purpose="customer_turn", turn_id=new_id())  # type: ignore[arg-type]
    gw = registry.require(ModelGatewayPort)  # type: ignore[type-abstract]

    result = await gw.chat("customer_desk", [ChatMessage.user("truffle price?")], ctx=ctx)
    assert (result.text, result.model, result.hedged) == ("fallback answer", cfg.fallback, True)
    assert fake.calls[0].pre_delay_s == 4.0  # the flag reached the primary attempt

    async with db.tenant_session(bid) as s:  # type: ignore[arg-type]
        run_id = await agent.record_run(
            s, ctx, desk="customer", prompt_version="test-v1", model_primary=cfg.primary, latency_ms=result.latency_ms
        )
    async with db.service_session(ServiceReason.ADMIN) as s:
        run = (await s.execute(text("SELECT * FROM agent_runs WHERE id = :id"), {"id": run_id})).one()
        events = (
            await s.execute(
                text("SELECT status, attrs FROM pipeline_events WHERE stage = 'model_call' AND turn_id = :t"),
                {"t": ctx.turn_id},
            )
        ).all()
    assert (run.model_primary, run.model_used, run.hedged, run.model_calls) == (cfg.primary, cfg.fallback, True, 1)
    assert run.latency_breakdown["model_calls"][0]["model"] == cfg.fallback
    assert len(events) == 1 and events[0].attrs["model"] == cfg.fallback and events[0].attrs["hedged"] is True


async def test_failed_call_emits_error_event(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    cfg = agent.load_models_config().role("classifier")
    fake.reply(cfg.primary, agent.FakeReply(error=agent.ModelError("PROVIDER_UNAVAILABLE", "503")))
    bid = await make_business(db)
    ctx = CallCtx(business_id=bid, purpose="scope", turn_id=new_id())  # type: ignore[arg-type]
    with pytest.raises(agent.ModelError):
        await agent.gateway().chat("classifier", [ChatMessage.user("x")], ctx=ctx)
    async with db.service_session(ServiceReason.ADMIN) as s:
        [ev] = (
            await s.execute(
                text("SELECT status, attrs FROM pipeline_events WHERE stage = 'model_call' AND turn_id = :t"),
                {"t": ctx.turn_id},
            )
        ).all()
    assert str(ev.status) == "error" and ev.attrs["code"] == "PROVIDER_UNAVAILABLE"


async def test_gateway_embedder_powers_knowledge_vectors(registry: Registry, db: Database) -> None:
    assert registry.optional(EmbedderPort) is not None  # type: ignore[type-abstract]
    bid = new_id()
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO businesses (id, name, slug, status) VALUES (:id, 'K', :s, 'active')"),
            {"id": bid, "s": f"k-{bid}"},
        )
    await knowledge.apply_now(
        bid, "admin", "catalogue", [("offering", "create", None, {"name": "Gooey Fudge Brownies"})]
    )
    from reflexo.knowledge import indexer

    embedder = registry.require(EmbedderPort)  # type: ignore[type-abstract]
    assert await indexer.embed_pending(bid, embedder) == 1
    async with db.tenant_session(bid) as s:
        model = (await s.execute(text("SELECT embedding_model FROM search_documents"))).scalar_one()
    assert model == agent.load_models_config().embedding.model
    assert (await knowledge.search_catalog(bid, "fudge"))[0].name == "Gooey Fudge Brownies"


def test_unconfigured_provider_does_not_provide_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from reflexo.common.config import get_settings

    monkeypatch.setenv("REFLEXO_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("REFLEXO_GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    try:
        reg = reset_registry()
        agent.register(reg)
        assert reg.optional(ModelGatewayPort) is not None  # type: ignore[type-abstract]
        assert reg.optional(EmbedderPort) is None  # type: ignore[type-abstract]  # search stays lexical
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
