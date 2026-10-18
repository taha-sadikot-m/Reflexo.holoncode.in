"""Model-call metering, cost caps, the rupee rate and X-ray turn cost on a real database (WP-01; BETA_12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import reflexo.agent.api as agent
from reflexo.admin import views
from reflexo.agent import cost_jobs, metering
from reflexo.agent.model_gateway.config import EmbeddingConfig, ModelsConfig, RoleConfig
from reflexo.common.costs import Usage, forget_fx_cache, price_book
from reflexo.common.db import Database, ServiceReason
from reflexo.common.enums import ModelCallKind
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx, ChatMessage, ModelError, ToolCall
from reflexo.common.ports import Registry
from reflexo.common.timeutil import FixedClock, SystemClock, set_clock
from reflexo.voice.metering import VoiceUsageMeter
from tests.factories import make_tenant
from tests.integration import test_customer_desk as cd
from tests.integration.test_customer_desk import PRIMARY, desk, make_bakery, reply, tool_results, turn_for

# the customer-desk tests' fixtures: a fake model, the real pipeline and gateway, a generous speculative budget
fake = cd.fake
notes = cd.notes
registry = cd.registry
_generous_speculative_budget = cd._generous_speculative_budget

pytestmark = pytest.mark.integration
APPEND_ONLY = (
    "credential_access_log",
    "model_calls",
    "tool_calls",
    "processing_log",
    "operator_access_log",
    "webhook_deliveries",
)  # fmt: skip  (BETA_02 §6; the others arrive with their work packages)


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


@pytest.fixture(autouse=True)
def _fresh_fx_cache() -> Any:
    forget_fx_cache()
    yield
    forget_fx_cache()
    set_clock(SystemClock())


# ---------------------------------------------------------------- every call has a priced row


@pytest.mark.check("CHK-ARCH-3", "CHK-PC-1", "CHK-PC-3")
async def test_a_turn_meters_every_model_call_and_xray_shows_their_sum(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    _, pnid = await make_bakery(db)
    fake.queue(
        PRIMARY,
        agent.FakeReply(
            tool_calls=[ToolCall(id="t1", name="delivery_quote", args={"area_or_pincode": "Karve Nagar"})],
            input_tokens=5800,
            cached_input_tokens=2000,
            output_tokens=60,
        ),
    )

    def second(a: agent.Attempt) -> agent.FakeReply:
        quote = tool_results(a)[-1]["data"]
        r = reply("send", f"Delivery {quote['fee']}", topic="delivery_shipping",
                  refs=[{"type": "delivery_zone", "id": quote["zone_id"]}])  # fmt: skip
        return agent.FakeReply(tool_calls=r.tool_calls, input_tokens=6300, cached_input_tokens=5000, output_tokens=120)

    fake.script(PRIMARY, second)
    handle = await turn_for(registry, db, pnid, "919811100071", "Karve nagar mein deliver karte ho?")
    await desk(registry).run_turn(handle)

    turn = handle.ref.turn_id
    rows = await q(db, "SELECT * FROM model_calls WHERE turn_id = :t AND kind = 'chat' ORDER BY created_at", t=turn)
    assert len(rows) == 2  # one row per model call of the turn
    assert [r.input_tokens for r in rows] == [5800, 6300] and [r.cached_input_tokens for r in rows] == [2000, 5000]
    for r in rows:
        assert r.cost_micro_inr > 0 and r.price_version == price_book().version and r.fx_rate > 0
        assert (r.feature, r.purpose, r.provider, r.status) == ("chat", "customer_turn", "fake", "ok")
        assert r.prompt_version.startswith("customer@")
    total = sum(
        r.cost_micro_inr for r in await q(db, "SELECT cost_micro_inr FROM model_calls WHERE turn_id = :t", t=turn)
    )

    [event] = await q(db, "SELECT trace_id FROM pipeline_events WHERE turn_id = :t LIMIT 1", t=turn)
    detail = await views.trace_detail(handle.ref.business_id, event.trace_id)
    assert detail is not None
    [summary] = [t for t in detail["turns"] if t["turn_id"] == str(turn)]
    assert summary["cost_micro_inr"] == total  # X-ray shows exactly the rows' sum
    assert summary["model_calls"] == 2 and summary["cached_input_tokens"] == 7000


@pytest.mark.check("CHK-ARCH-3")
async def test_failed_attempts_timeouts_and_embeddings_are_metered(db: Database) -> None:
    t = await make_tenant(db, "Meter")
    fake = agent.FakeProvider()
    billed = {"input_tokens": 4000, "output_tokens": 0}
    fake.queue("gemini-2.5-flash", agent.FakeReply(error=ModelError("INVALID_OUTPUT", "empty", usage=billed)))
    fake.queue("gemini-2.5-flash-lite", agent.FakeReply(first_token_ms=500, total_ms=500))
    cfg = ModelsConfig(
        roles={
            "solo": RoleConfig(primary="gemini-2.5-flash", timeout_ms=5000),
            "slow": RoleConfig(primary="gemini-2.5-flash-lite", timeout_ms=100),
        },
        embedding=EmbeddingConfig(model="gemini-embedding-001"),
    )
    gw = agent.ModelGateway(fake, cfg)
    ctx = CallCtx(business_id=t.business_id, purpose="answer_scope")
    with pytest.raises(ModelError):
        await gw.chat("solo", [ChatMessage.user("x")], ctx=ctx)
    with pytest.raises(ModelError, match="TIMEOUT"):
        await gw.chat("slow", [ChatMessage.user("x")], ctx=ctx)
    await gw.embed(["Chocolate truffle cake 1 kg"], task="query", ctx=CallCtx(t.business_id, "embedding"))

    rows = {r.status: r for r in await q(db, "SELECT * FROM model_calls WHERE business_id = :b", b=t.business_id)}
    assert rows["error"].input_tokens == 4000 and rows["error"].error_code == "INVALID_OUTPUT"
    assert rows["error"].cost_micro_inr > 0  # an empty answer still bills its input
    assert rows["timeout"].error_code == "TIMEOUT" and rows["timeout"].cost_micro_inr == 0
    assert str(rows["ok"].kind) == "embed" and rows["ok"].characters == 27 and rows["ok"].input_tokens == 7


@pytest.mark.check("CHK-ARCH-3")
async def test_a_voice_call_writes_a_row_per_item(db: Database) -> None:
    t = await make_tenant(db, "Voice")
    meter = VoiceUsageMeter(t.business_id, new_id())
    meter._since -= 30  # a 30-second call
    meter.add(type("LLMMetrics", (), {"prompt_tokens": 3000, "prompt_cached_tokens": 1000, "completion_tokens": 80})())
    meter.add(type("STTMetrics", (), {"audio_duration": 42.5})())
    meter.add(type("TTSMetrics", (), {"characters_count": 650})())
    meter.add(type("EOUMetrics", (), {})())  # no usage
    await meter.flush()
    rows = {str(r.kind): r for r in await q(db, "SELECT * FROM model_calls WHERE business_id = :b", b=t.business_id)}
    assert set(rows) == {"voice_session", "voice_llm", "voice_stt", "voice_tts"}
    assert all(r.feature == "voice" and r.run_ref.startswith("call:") and r.cost_micro_inr >= 0 for r in rows.values())
    assert rows["voice_llm"].input_tokens == 3000 and rows["voice_stt"].audio_seconds == Decimal("42.50")
    assert rows["voice_tts"].characters == 650 and rows["voice_tts"].cost_micro_inr > 0


# ---------------------------------------------------------------- append-only


@pytest.mark.check("CHK-DB-8")
async def test_append_only_tables_cannot_be_changed(db: Database) -> None:
    t = await make_tenant(db, "Append")
    ctx = CallCtx(business_id=t.business_id, purpose="answer_scope")
    await metering.record(ctx, kind=ModelCallKind.CHAT, provider="gemini", model="gemini-2.5-flash",
                          usage=Usage(input_tokens=10), duration_ms=5)  # fmt: skip
    existing = {
        r.table_name
        for r in await q(db, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    }
    tables = [name for name in APPEND_ONLY if name in existing]
    assert "model_calls" in tables
    for table in tables:
        for sql in (f"UPDATE {table} SET business_id = business_id", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with db.tenant_session(t.business_id) as s:
                    await s.execute(text(sql))
            with pytest.raises(DBAPIError, match="permission denied"):
                async with db.service_session(ServiceReason.ADMIN) as s:
                    await s.execute(text(sql))


# ---------------------------------------------------------------- the daily counter and the 80% alert


async def test_counter_falls_back_to_the_rows_and_the_alert_fires_once(db: Database) -> None:
    t = await make_tenant(db, "Cap")
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("INSERT INTO cost_caps (business_id, daily_cap_micro_inr) VALUES (:b, 50000)"), {"b": t.business_id}
        )
    ctx = CallCtx(business_id=t.business_id, purpose="customer_turn")

    async def call() -> None:  # about 17,100 micro-rupees each: the 80% line (40,000) is crossed on the third
        await metering.record(ctx, kind=ModelCallKind.CHAT, provider="gemini", model="gemini-2.5-flash-lite",
                              usage=Usage(input_tokens=1000, output_tokens=200), duration_ms=10)  # fmt: skip

    async def alerted() -> Any:
        return (await q(db, "SELECT alerted_on FROM cost_caps WHERE business_id = :b", b=t.business_id))[0].alerted_on

    await call()
    await call()
    assert await alerted() is None
    rows = await q(db, "SELECT cost_micro_inr FROM model_calls WHERE business_id = :b", b=t.business_id)
    assert await metering.spent_today(t.business_id) == sum(r.cost_micro_inr for r in rows)  # Redis is off
    await call()
    assert await alerted() is not None
    await call()
    alerts = await q(db, "SELECT attrs FROM pipeline_events WHERE business_id = :b AND stage = 'cost_cap'",
                     b=t.business_id)  # fmt: skip
    assert len(alerts) == 1 and alerts[0].attrs["cap_micro_inr"] == 50000


# ---------------------------------------------------------------- the rupee rate


def _transport(answers: dict[str, Any]) -> httpx.AsyncClient:
    def handle(req: httpx.Request) -> httpx.Response:
        body = answers.get(req.url.host)
        return httpx.Response(503, text="down") if body is None else httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.check("CHK-PC-8")
async def test_fx_refresh_falls_back_then_carries_forward_then_alerts(db: Database) -> None:
    clock = FixedClock(datetime(2031, 3, 1, 1, 0, tzinfo=UTC))  # 06:30 IST, far from other tests' dates
    set_clock(clock)
    frankfurter = {"amount": 1.0, "base": "USD", "date": "2031-03-01", "rates": {"INR": 96.12}}
    er_api = {"result": "success", "base_code": "USD", "rates": {"INR": 96.40, "EUR": 0.9}}

    first = await cost_jobs.fx_refresh(_transport({"api.frankfurter.dev": frankfurter, "open.er-api.com": er_api}))
    assert (first["source"], first["usd_inr"]) == ("frankfurter", "96.1200")

    clock.advance(timedelta(days=1))
    second = await cost_jobs.fx_refresh(_transport({"open.er-api.com": er_api}))
    assert (second["source"], second["usd_inr"]) == ("open_er_api", "96.4000")

    alerts_before = len(await q(db, "SELECT 1 FROM pipeline_events WHERE stage = 'alert'"))
    for day in range(1, 4):
        clock.advance(timedelta(days=1))
        carried = await cost_jobs.fx_refresh(_transport({}))
        assert (carried["source"], carried["usd_inr"], carried["carried_days"]) == ("carried_forward", "96.4000", day)
        alerts = await q(db, "SELECT attrs FROM pipeline_events WHERE stage = 'alert' ORDER BY created_at")
        assert len(alerts) - alerts_before == (1 if day == 3 else 0)  # the operator hears on the third day
    assert alerts[-1].attrs["kind"] == "fx_refresh_failing"

    clock.advance(timedelta(days=1))
    assert (await cost_jobs.fx_refresh(_transport({"api.frankfurter.dev": frankfurter})))["carried_days"] == 0


async def test_rollup_rebuilds_cost_daily_from_the_rows(db: Database) -> None:
    t = await make_tenant(db, "Rollup")
    for purpose in ("customer_turn", "customer_turn", "automation.build"):
        await metering.record(CallCtx(business_id=t.business_id, purpose=purpose), kind=ModelCallKind.CHAT,
                              provider="gemini", model="gemini-2.5-flash", usage=Usage(input_tokens=100),
                              duration_ms=1)  # fmt: skip
    await cost_jobs.rollup()
    await cost_jobs.rollup()  # idempotent
    daily = {r.feature: r for r in await q(db, "SELECT * FROM cost_daily WHERE business_id = :b", b=t.business_id)}
    assert set(daily) == {"chat", "automation_build"} and daily["chat"].calls == 2
    rows = await q(db, "SELECT cost_micro_inr FROM model_calls WHERE business_id = :b", b=t.business_id)
    assert sum(d.cost_micro_inr for d in daily.values()) == sum(r.cost_micro_inr for r in rows)


async def test_daily_jobs_schedule_today_once_and_then_tomorrow(registry: Registry, db: Database) -> None:
    from reflexo.pipeline.jobs import Jobs

    jobs = registry.require(Jobs)
    await jobs.ensure_daily()
    await jobs.ensure_daily()  # a second worker or a restart changes nothing
    rows = await q(db, "SELECT queue, dedupe_key, payload FROM jobs WHERE queue LIKE 'costs.%' AND status = 'pending'")
    by_queue = {r.queue: r for r in rows}
    assert set(by_queue) == {"costs.fx_refresh", "costs.rollup"} and len(rows) == 2
    fx = by_queue["costs.fx_refresh"]
    assert fx.dedupe_key == f"costs.fx_refresh:{fx.payload['run_date']}"
    async with db.service_session(ServiceReason.ADMIN) as s:  # today's runs are over
        await s.execute(text("UPDATE jobs SET status = 'done' WHERE queue LIKE 'costs.%' AND status = 'pending'"))
    await jobs.ensure_daily()
    after = {
        r.queue: r
        for r in await q(db, "SELECT queue, payload FROM jobs WHERE queue LIKE 'costs.%' AND status = 'pending'")
    }
    assert set(after) == {"costs.fx_refresh", "costs.rollup"}
    assert after["costs.fx_refresh"].payload["run_date"] > fx.payload["run_date"]  # tomorrow's run
