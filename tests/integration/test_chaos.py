"""Chaos building blocks (POC_07 §5) on a real database. The full buttons need a running worker and model; they are
exercised against the dev stack. Here: RLS proof, signed simulated ingress + dedupe, flags, the one-shot kill."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

import reflexo.admin.api as admin
import reflexo.agent.api as agent
import reflexo.gateway.api as gateway_api
import reflexo.knowledge.api as knowledge
from reflexo.agent.tools import base as tools_base
from reflexo.common.db import Database, ServiceReason
from reflexo.common.flags import clear_caches
from reflexo.common.ids import new_id
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry, reset_registry
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_customer_desk import make_bakery

pytestmark = pytest.mark.integration


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=agent.FakeProvider())
    knowledge.register(reg)
    return reg


async def clear_flags(db: Database) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("DELETE FROM runtime_flags WHERE key IN ('redis_disabled', 'kill_after_tool')"))
    clear_caches()


async def test_cross_business_read_returns_nothing_and_insert_is_refused(registry: Registry, db: Database) -> None:
    meera, _ = await make_bakery(db)
    other, _ = await make_bakery(db)
    result = await admin.chaos.run("cross_business_read", meera, other)
    assert result.ok, result.checks
    assert result.checks["rows_returned"] == 0 and result.checks["rows_that_exist"] > 0
    assert "row-level security" in result.checks["insert_error"]
    [event] = await q(db, "SELECT kind, outcome FROM chaos_events WHERE business_id = :b", b=meera)
    assert event.kind == "cross_business_read" and event.outcome["ok"] is True


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


async def test_simulated_customer_uses_the_signed_ingress_and_is_deduplicated(registry: Registry, db: Database) -> None:
    bid, _ = await make_bakery(db)
    mid = f"wamid.SIMTEST{new_id().hex}"
    raw = await admin.sim.customer_message_bytes(bid, 21, "hi", mid)
    statuses = await asyncio.gather(*(admin.sim.post_raw(raw) for _ in range(3)))
    assert statuses == [200, 200, 200]
    rows = await q(db, "SELECT simulated FROM inbox_events WHERE payload::text LIKE :k", k=f"%{mid}%")
    assert [r.simulated for r in rows] == [True]  # stored once, flagged simulated (replies go to the Lab)
    assert (await admin.sim.customer_says(bid, 21, text_="hello")).status == 200


async def test_redis_disabled_flag_fails_every_redis_call_over(db: Database) -> None:
    await admin.chaos.set_flag("redis_disabled", True, 30)
    try:
        with pytest.raises(RedisUnavailable):
            await get_redis().call("ping")
        flags = await admin.chaos.active_flags()
        assert flags["redis_disabled"]["value"] is True and flags["redis_disabled"]["expires_at"]
    finally:
        await clear_flags(db)


async def test_kill_after_tool_fires_once(registry: Registry, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    bid, _ = await make_bakery(db)
    exits: list[int] = []

    class Killed(Exception):
        pass

    def fake_exit(code: int) -> None:
        exits.append(code)
        raise Killed

    monkeypatch.setattr(tools_base.os, "_exit", fake_exit)
    await admin.chaos.set_flag("kill_after_tool", {"tool": "catalog_search"}, 60)
    ctx = agent.ToolContext(
        business_id=bid,
        desk="customer",
        channel="whatsapp",
        conversation_id=None,
        customer_id=None,
        member_id=None,
        turn_id=new_id(),
    )
    call = ToolCall(id="1", name="catalog_search", args={"query": "truffle"})
    with pytest.raises(Killed):
        await agent.run_tool(call, ctx, {"catalog_search"})
    clear_caches()
    assert (await agent.run_tool(call, ctx, {"catalog_search"}))["ok"]  # the retry runs normally
    assert exits == [137]
    await clear_flags(db)


async def test_start_returns_at_once_and_the_run_closes_its_row(registry: Registry, db: Database) -> None:
    meera, _ = await make_bakery(db)
    await make_bakery(db)
    run_id = await admin.chaos.start("cross_business_read", meera)
    [first] = await admin.chaos.recent(meera)
    assert first["id"] == str(run_id) and first["state"] in ("running", "done")
    await asyncio.gather(*admin.chaos._tasks)
    [done] = await admin.chaos.recent(meera)
    assert done["state"] == "done" and done["outcome"]["ok"] is True


async def test_one_run_at_a_time_per_business_unless_the_open_row_is_stale(registry: Registry, db: Database) -> None:
    bid, _ = await make_bakery(db)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("INSERT INTO chaos_events (id, business_id, kind, params) VALUES (:i, :b, 'burst', '{}')"),
            {"i": new_id(), "b": bid},
        )
    with pytest.raises(admin.chaos.ChaosBusy):
        await admin.chaos.start("cross_business_read", bid)
    async with db.service_session(ServiceReason.ADMIN) as s:  # API restarted mid-run: the row is orphaned
        await s.execute(
            text("UPDATE chaos_events SET created_at = now() - interval '11 minutes' WHERE business_id = :b"),
            {"b": bid},
        )
    assert (await admin.chaos.recent(bid))[0]["state"] == "abandoned"
    await admin.chaos.start("cross_business_read", bid)
    await asyncio.gather(*admin.chaos._tasks)


async def test_a_failing_run_still_closes_its_row(
    registry: Registry, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    bid, _ = await make_bakery(db)

    async def boom(*_: Any) -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(admin.chaos, "_cross_business", boom)
    await admin.chaos.start("cross_business_read", bid)
    await asyncio.gather(*admin.chaos._tasks)
    [row] = await admin.chaos.recent(bid)
    assert row["state"] == "done" and row["outcome"]["ok"] is False
    assert "provider exploded" in row["outcome"]["checks"]["error"]


async def test_disarm_clears_only_chaos_flags(db: Database) -> None:
    await admin.chaos.set_flag("redis_disabled", True, 30)
    assert await admin.chaos.clear_flag("redis_disabled") is True
    assert "redis_disabled" not in await admin.chaos.active_flags()
    assert await admin.chaos.clear_flag("redis_disabled") is False
    with pytest.raises(ValueError):
        await admin.chaos.clear_flag("kill_switch")
