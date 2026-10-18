"""Benchmark measures come from stored data (WP-01; CHK-ARCH-6; BETA_01 §7.1, BETA_12 §2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
from reflexo.admin import benchmark
from reflexo.common.db import Database, ServiceReason
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.llm import ToolCall
from reflexo.common.ports import Registry
from reflexo.common.timeutil import utcnow
from reflexo.common.xray import emit
from tests.integration import test_customer_desk as cd
from tests.integration.test_customer_desk import PRIMARY, desk, make_bakery, reply, tool_results, turn_for

pytestmark = pytest.mark.integration
fake = cd.fake
notes = cd.notes
registry = cd.registry
_generous_speculative_budget = cd._generous_speculative_budget


async def _times(db: Database, turn: UUID, inbound_at: Any, reply_after_ms: int) -> None:
    """Pin when the customer wrote and when the provider accepted the reply: the only inputs to reply time."""
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""UPDATE messages SET created_at = :at WHERE id = ANY(
                      SELECT unnest(message_ids) FROM conversation_turns WHERE id = :t)"""),
            {"at": inbound_at, "t": turn},
        )
        await s.execute(
            text("""UPDATE outbound_messages o SET sent_at = :sent FROM messages m
                    WHERE m.id = o.message_id AND m.turn_id = :t"""),
            {"sent": inbound_at + timedelta(milliseconds=reply_after_ms), "t": turn},
        )


@pytest.mark.check("CHK-ARCH-6")
async def test_measures_are_read_from_stored_data(registry: Registry, db: Database, fake: agent.FakeProvider) -> None:
    since = utcnow() - timedelta(seconds=1)
    bid, pnid = await make_bakery(db)

    def price(a: agent.Attempt) -> agent.FakeReply:
        card = cd.find_card(a, "Pineapple Cake")
        assert card is not None
        v = card["variants"][0]
        return reply(
            "send", f"Pineapple 1 kg {v['price']}", topic="pricing", refs=[{"type": "variant", "id": v["variant_id"]}]
        )

    fake.script(PRIMARY, price)
    first = await turn_for(registry, db, pnid, "919811100091", "pineapple cake kitne ka hai")
    await desk(registry).run_turn(first)

    fake.queue(
        PRIMARY,
        agent.FakeReply(tool_calls=[ToolCall(id="d1", name="delivery_quote", args={"area_or_pincode": "Warje"})]),
    )

    def quote(a: agent.Attempt) -> agent.FakeReply:
        q = tool_results(a)[-1]["data"]
        return reply(
            "send",
            f"Delivery {q['fee']}",
            topic="delivery_shipping",
            refs=[{"type": "delivery_zone", "id": q["zone_id"]}],
        )

    fake.script(PRIMARY, quote)
    second = await turn_for(registry, db, pnid, "919811100091", "Warje delivery?")
    await desk(registry).run_turn(second)

    t0 = utcnow() - timedelta(minutes=1)
    await _times(db, first.ref.turn_id, t0, 2400)
    # written 2 s in, before the first reply went out at 2.4 s: a split burst
    await _times(db, second.ref.turn_id, t0 + timedelta(seconds=2), 4000)
    for ref, queries, wait in ((first.ref, 30, 600), (second.ref, 50, 400)):
        await emit(
            "turn_started", PipelineEventStatus.START, business_id=bid, turn_id=ref.turn_id, coalesce_wait_ms=wait
        )
        await emit("turn_finished", PipelineEventStatus.OK, business_id=bid, turn_id=ref.turn_id, db_queries=queries)

    result = await benchmark.measure(bid, since, utcnow())
    m = result.measures
    assert (m["chat_reply_ms"].median, m["chat_reply_ms"].n) == (3200.0, 2)  # (2,400 + 4,000) / 2
    assert m["model_calls_per_turn"].mean == 1.5 and m["one_call_turns"].mean == 0.5
    assert m["split_burst_rate"].mean == 0.5 and result.unmetered_turns == 0
    assert (m["db_queries_per_turn"].median, m["coalesce_wait_ms"].median) == (40.0, 500.0)
    async with db.service_session(ServiceReason.ADMIN) as s:
        costs = (
            (
                await s.execute(
                    text("SELECT sum(cost_micro_inr) FROM model_calls WHERE turn_id = ANY(:t) GROUP BY turn_id"),
                    {"t": [first.ref.turn_id, second.ref.turn_id]},
                )
            )
            .scalars()
            .all()
        )
    assert m["cost_per_chat_turn_micro_inr"].median == sum(int(c) for c in costs) / 2  # the median of two turns
    assert result.spent_micro_inr >= sum(costs) and result.feature_cost["chat"]["micro_inr_per_turn"] > 0
    measured = set(m) | set(result.not_available)
    for needed in ("chat_reply_ms", "typing_ms", "voice_start_speaking_ms", "voice_call_ready_ms", "comment_to_dm_ms",
                   "cost_per_chat_turn_micro_inr", "cost_per_voice_minute_micro_inr"):  # fmt: skip
        assert needed in measured  # every §7.1 measure is reported, or named as not available yet
