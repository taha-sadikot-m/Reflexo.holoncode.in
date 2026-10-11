"""Pipeline mechanics on a real database (POC_03 §1, §2, §4, §7; checks CHK-F01-2..6)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    DomainEventWriter,
    InboundProcessorPort,
    InboxEvent,
    NewDomainEvent,
    OutboundRow,
    OutboundSenderPort,
    SendResult,
    SendStatus,
    Superseded,
    TurnHandle,
    TurnRef,
    TurnRunnerPort,
    TurnScheduler,
)
from reflexo.common.db import Database, ServiceReason
from reflexo.common.enums import Desk, PriorityClass, TurnOutcome
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.pipeline import outbound
from reflexo.pipeline.jobs import Jobs
from reflexo.pipeline.turns import PipelineTurnHandle, TurnService
from reflexo.pipeline.worker import Worker
from tests.factories import Tenant, make_inbound_message, make_outbound, make_tenant

pytestmark = pytest.mark.integration


@pytest.fixture
def registry() -> Registry:
    reg = reset_registry()
    pipeline_api.register(reg)
    return reg


async def _turn(db: Database, turn_id: UUID) -> Any:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return (await s.execute(text("SELECT * FROM conversation_turns WHERE id = :id"), {"id": turn_id})).one()


async def _schedule(registry: Registry, db: Database, t: Tenant, mid: UUID, ends_thought: bool = False) -> UUID:
    scheduler = registry.require(TurnScheduler)  # type: ignore[type-abstract]
    async with db.tenant_session(t.business_id) as s:
        return await scheduler.schedule(
            s,
            business_id=t.business_id,
            conversation_id=t.conversation_id,
            message_id=mid,
            desk=Desk.CUSTOMER,
            priority=PriorityClass.P0,
            ends_thought=ends_thought,
        )


# ------------------------------------------------------------------ inbox


async def test_inbox_store_is_idempotent(db: Database) -> None:
    key = str(new_id())
    first = await pipeline_api.store_inbox_event("simulator", key, route_key="r", headers={}, payload={"a": 1})
    second = await pipeline_api.store_inbox_event("simulator", key, route_key="r", headers={}, payload={"a": 1})
    assert first.inserted and not second.inserted


# ------------------------------------------------------------------ jobs


async def test_jobs_dedupe_retry_and_dead_letter(db: Database, registry: Registry) -> None:
    jobs = registry.require(Jobs)
    key = f"k-{new_id()}"
    jid = await jobs.enqueue("test.queue", {"x": 1}, dedupe_key=key, max_attempts=2)
    assert jid is not None
    assert await jobs.enqueue("test.queue", {"x": 1}, dedupe_key=key) is None  # deduped while pending

    claimed = [j for j in await jobs.claim("w1", 30, 100_000) if j.id == jid]  # other tests leave jobs pending
    assert claimed and claimed[0].attempts == 1
    assert await jobs.fail(claimed[0], "w1", "boom") == "pending"
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": jid})
    again = [j for j in await jobs.claim("w1", 30, 100_000) if j.id == jid]
    assert again and again[0].attempts == 2
    assert await jobs.fail(again[0], "w1", "boom") == "dead"


# ------------------------------------------------------------------ turns


async def test_burst_coalesces_into_one_turn(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    mids = [await make_inbound_message(db, t, f"m{i}") for i in range(3)]
    turn_ids = {await _schedule(registry, db, t, m) for m in mids}
    assert len(turn_ids) == 1
    row = await _turn(db, turn_ids.pop())
    assert sorted(row.message_ids) == sorted(mids)
    window_cap = get_settings().coalesce_cap_ms / 1000
    assert (row.run_after - row.first_message_at).total_seconds() <= window_cap + 0.01


async def test_superseded_turn_cannot_commit(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    service = registry.require(TurnService)
    m1 = await make_inbound_message(db, t, "price?")
    await _schedule(registry, db, t, m1, ends_thought=True)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE conversation_turns SET run_after = now() WHERE conversation_id = :c"), {"c": t.conversation_id}
        )
    running = next(r for r in await service.claim_due("w1", 50) if r.conversation_id == t.conversation_id)
    handle = PipelineTurnHandle(running, "w1", service)

    m2 = await make_inbound_message(db, t, "aur M size?")
    pending_id = await _schedule(registry, db, t, m2)

    assert await handle.is_superseded()
    with pytest.raises(Superseded):
        await handle.checkpoint("model")
    wrote: list[bool] = []

    async def writer(_s: Any) -> None:
        wrote.append(True)

    handle._superseded = False  # bypass the cache: commit must re-check under the row lock
    with pytest.raises(Superseded):
        await handle.commit_reply(writer, TurnOutcome.REPLIED)
    assert wrote == []  # nothing stale written
    pending = await _turn(db, pending_id)
    assert set(pending.message_ids) == {m1, m2}
    assert running.turn_id in pending.inherited_turn_ids


async def test_committed_turn_is_not_superseded(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    service = registry.require(TurnService)
    m1 = await make_inbound_message(db, t)
    await _schedule(registry, db, t, m1, ends_thought=True)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE conversation_turns SET run_after = now() WHERE conversation_id = :c"), {"c": t.conversation_id}
        )
    ref = next(r for r in await service.claim_due("w1", 50) if r.conversation_id == t.conversation_id)
    handle = PipelineTurnHandle(ref, "w1", service)

    async def writer(_s: Any) -> None:
        return None

    await handle.commit_reply(writer, TurnOutcome.REPLIED)
    m2 = await make_inbound_message(db, t)
    new_pending = await _schedule(registry, db, t, m2)
    done = await _turn(db, ref.turn_id)
    assert done.status == "completed" and done.superseded_by is None
    assert (await _turn(db, new_pending)).message_ids == [m2]


async def test_crashed_turn_is_recovered_then_exhausted(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    service = registry.require(TurnService)
    await _schedule(registry, db, t, await make_inbound_message(db, t), ends_thought=True)
    for attempt in range(1, get_settings().turn_max_attempts + 1):
        async with db.service_session(ServiceReason.ADMIN) as s:
            await s.execute(
                text("UPDATE conversation_turns SET run_after = now() WHERE conversation_id = :c"),
                {"c": t.conversation_id},
            )
        ref = next(r for r in await service.claim_due(f"w{attempt}", 50) if r.conversation_id == t.conversation_id)
        assert ref.attempts == attempt
        async with db.service_session(ServiceReason.ADMIN) as s:  # the worker "dies": lease expires
            await s.execute(
                text("UPDATE conversation_turns SET lease_until = now() - interval '1 second' WHERE id = :id"),
                {"id": ref.turn_id},
            )
        exhausted = await service.sweep_expired()
    assert [e.turn_id for e in exhausted] == [ref.turn_id]
    final = await _turn(db, ref.turn_id)
    assert final.status == "failed" and final.outcome == "fallback"


# ------------------------------------------------------------------ outbound


async def test_outbound_parts_are_strictly_ordered(db: Database) -> None:
    t = await make_tenant(db)
    _, parts = await make_outbound(db, t, parts=3)
    claimed = [r for r in await outbound.claim("w1", 30, 50) if r.conversation_id == t.conversation_id]
    assert [r.id for r in claimed] == [parts[0]]
    assert await outbound.apply_result(claimed[0], "w1", SendResult(SendStatus.SENT, "p-0")) == "sent"
    claimed = [r for r in await outbound.claim("w1", 30, 50) if r.conversation_id == t.conversation_id]
    assert [r.id for r in claimed] == [parts[1]]


async def test_unknown_outcome_waits_for_status_then_confirms(db: Database) -> None:
    t = await make_tenant(db)
    _, parts = await make_outbound(db, t, parts=1)
    row = next(r for r in await outbound.claim("w1", 30, 50) if r.id == parts[0])
    result = SendResult(SendStatus.UNKNOWN, provider_message_id="pm-1", error_message="read timeout")
    assert await outbound.apply_result(row, "w1", result) == "unconfirmed"
    assert not [r for r in await outbound.claim("w1", 30, 50) if r.id == parts[0]]  # waits ~10 s
    # The adapter persisted the provider id before timing out; the delivery webhook now confirms it.
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE outbound_messages SET provider_message_id = 'pm-1' WHERE id = :id"), {"id": parts[0]}
        )
    assert await outbound.confirm_by_provider_id("pm-1", "delivered")


async def test_unknown_outcome_no_retry_when_provider_forbids(db: Database) -> None:
    t = await make_tenant(db)
    _, parts = await make_outbound(db, t, parts=1)
    row = next(r for r in await outbound.claim("w1", 30, 50) if r.id == parts[0])
    result = SendResult(SendStatus.UNKNOWN, error_message="timeout", retry_after_unknown_allowed=False)
    await outbound.apply_result(row, "w1", result)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text("UPDATE outbound_messages SET next_attempt_at = now() WHERE id = :id"), {"id": parts[0]})
    assert not [r for r in await outbound.claim("w1", 30, 50) if r.id == parts[0]]  # final: never re-sent


# ------------------------------------------------------------------ domain events


async def test_loop_guard_drops_deep_automation_events(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    writer = registry.require(DomainEventWriter)  # type: ignore[type-abstract]
    async with db.tenant_session(t.business_id) as s:
        ok = await writer.append(
            s,
            NewDomainEvent(
                t.business_id, "order.created", "automation:x", {}, correlation_id=new_id(), causation_depth=2
            ),
        )
        dropped = await writer.append(
            s,
            NewDomainEvent(
                t.business_id, "order.created", "automation:x", {}, correlation_id=new_id(), causation_depth=3
            ),
        )
    assert ok is not None and dropped is None


# ------------------------------------------------------------------ worker end-to-end


class FakeInbound:
    def __init__(self, registry: Registry, db: Database, tenant: Tenant) -> None:
        self.registry, self.db, self.t = registry, db, tenant

    async def process_inbox_event(self, event: InboxEvent) -> None:
        if event.payload.get("tenant") != str(self.t.business_id):
            return  # rows left by other tests in the shared database
        mid = await make_inbound_message(self.db, self.t, event.payload["text"])
        await _schedule(self.registry, self.db, self.t, mid, ends_thought=True)


class FakeRunner:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.turns: list[TurnRef] = []

    async def run_turn(self, handle: TurnHandle) -> None:
        self.turns.append(handle.ref)
        await handle.checkpoint("model")

        async def write(s: Any) -> None:
            mid, oid = new_id(), new_id()
            await s.execute(
                text("""INSERT INTO messages (id, business_id, conversation_id, direction, author, channel,
                                        channel_connection_id, content_text, status, turn_id)
                                    SELECT :id, business_id, id, 'outbound', 'ai', channel, channel_connection_id,
                                           'reply', 'queued', :t FROM conversations WHERE id = :c"""),
                {"id": mid, "t": handle.ref.turn_id, "c": handle.ref.conversation_id},
            )
            await s.execute(
                text("""INSERT INTO outbound_messages (id, business_id, message_id, conversation_id,
                                        channel_connection_id, idempotency_key, rendered_payload, priority)
                                    SELECT :oid, business_id, :mid, id, channel_connection_id, :k, '{}', 'p0'
                                    FROM conversations WHERE id = :c"""),
                {"oid": oid, "mid": mid, "k": f"turn:{handle.ref.turn_id}:0", "c": handle.ref.conversation_id},
            )

        await handle.commit_reply(write, TurnOutcome.REPLIED)

    async def send_holding_reply(self, business_id: UUID, conversation_id: UUID, turn_id: UUID) -> None:
        return None

    async def run_fallback(self, ref: TurnRef, reason: str) -> None:
        return None


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[OutboundRow] = []

    async def send(self, row: OutboundRow) -> SendResult:
        self.sent.append(row)
        return SendResult(SendStatus.SENT, provider_message_id=f"prov-{row.id}")

    async def on_final_failure(self, row: OutboundRow, result: SendResult) -> None:
        return None


async def test_worker_end_to_end_inbox_to_sent(db: Database, registry: Registry) -> None:
    t = await make_tenant(db)
    runner, sender = FakeRunner(db), FakeSender()
    registry.provide(InboundProcessorPort, FakeInbound(registry, db, t))  # type: ignore[type-abstract]
    registry.provide(TurnRunnerPort, runner)  # type: ignore[type-abstract]
    registry.provide(OutboundSenderPort, sender)  # type: ignore[type-abstract]
    worker = Worker(registry)
    task = asyncio.create_task(worker.run())
    try:
        for text_body in ("hi", "price?"):
            await pipeline_api.store_inbox_event(
                "simulator",
                str(new_id()),
                route_key="r",
                headers={},
                payload={"text": text_body, "tenant": str(t.business_id)},
            )
        for _ in range(100):
            if [r for r in sender.sent if r.conversation_id == t.conversation_id]:
                break
            await asyncio.sleep(0.1)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=20)
    turns = [r for r in runner.turns if r.conversation_id == t.conversation_id]
    sent = [r for r in sender.sent if r.conversation_id == t.conversation_id]
    assert len(turns) == 1, "two quick messages must produce one turn"
    assert len(turns[0].message_ids) == 2
    assert len(sent) == 1
    async with db.service_session(ServiceReason.ADMIN) as s:
        status = (
            await s.execute(text("SELECT status FROM outbound_messages WHERE id = :id"), {"id": sent[0].id})
        ).scalar_one()
    assert status == "sent"


async def test_holding_reply_fires_only_for_a_still_running_turn(db: Database, registry: Registry) -> None:
    from reflexo.common.contracts import JobHandlerRegistry, JobRow
    from reflexo.pipeline.turns import HOLDING_QUEUE

    t = await make_tenant(db)
    service = registry.require(TurnService)
    calls: list[UUID] = []

    class Runner(FakeRunner):
        async def send_holding_reply(self, business_id: UUID, conversation_id: UUID, turn_id: UUID) -> None:
            calls.append(turn_id)

    registry.provide(TurnRunnerPort, Runner(db))  # type: ignore[type-abstract]
    await _schedule(registry, db, t, await make_inbound_message(db, t), ends_thought=True)
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE conversation_turns SET run_after = now() WHERE conversation_id = :c"), {"c": t.conversation_id}
        )
    ref = next(r for r in await service.claim_due("w1", 50) if r.conversation_id == t.conversation_id)
    handler = registry.require(JobHandlerRegistry).get(HOLDING_QUEUE)  # type: ignore[type-abstract]
    assert handler is not None
    job = JobRow(new_id(), t.business_id, HOLDING_QUEUE, {"turn_id": str(ref.turn_id)}, 1, 5, None)
    await handler(job)
    assert calls == [ref.turn_id]

    await service.mark(ref, "completed", TurnOutcome.REPLIED)
    await handler(job)
    assert calls == [ref.turn_id]  # no holding reply once the turn has finished
