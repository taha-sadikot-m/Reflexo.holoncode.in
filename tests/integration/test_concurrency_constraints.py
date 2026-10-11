"""Constraint behaviour under concurrency: one pending turn (CHK-DB-4), exact counter limit (CHK-DB-5)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from reflexo.common.db import Database
from reflexo.common.ids import new_id
from tests.factories import make_tenant

pytestmark = pytest.mark.integration


async def test_only_one_pending_turn_per_conversation(db: Database) -> None:
    t = await make_tenant(db)

    async def insert_pending() -> bool:
        try:
            async with db.tenant_session(t.business_id) as s:
                await s.execute(
                    text("""
                    INSERT INTO conversation_turns (id, business_id, conversation_id, desk, priority, message_ids,
                                                    first_message_at, run_after)
                    VALUES (:id, :b, :c, 'customer', 'p0', ARRAY[:m]::uuid[], now(), now())"""),
                    {"id": new_id(), "b": t.business_id, "c": t.conversation_id, "m": new_id()},
                )
            return True
        except IntegrityError:
            return False

    results = await asyncio.gather(*(insert_pending() for _ in range(8)))
    assert results.count(True) == 1


async def test_counter_limit_is_exact_under_concurrency(db: Database) -> None:
    t = await make_tenant(db)
    automation_id = new_id()
    async with db.tenant_session(t.business_id) as s:
        await s.execute(
            text("INSERT INTO automations (id, business_id, name, description_nl) VALUES (:id, :b, 'cap', 'cap')"),
            {"id": automation_id, "b": t.business_id},
        )

    async def take() -> int | None:
        async with db.tenant_session(t.business_id) as s:
            row = (
                await s.execute(
                    text("""
                INSERT INTO automation_counters (automation_id, business_id, counter_key, value, limit_value)
                VALUES (:a, :b, 'cakes:2026-10-15', 1, 8)
                ON CONFLICT (automation_id, counter_key) DO UPDATE SET value = automation_counters.value + 1
                WHERE automation_counters.value < automation_counters.limit_value
                RETURNING value"""),
                    {"a": automation_id, "b": t.business_id},
                )
            ).scalar_one_or_none()
            return row

    values = await asyncio.gather(*(take() for _ in range(12)))
    accepted = sorted(v for v in values if v is not None)
    assert accepted == list(range(1, 9))
    assert values.count(None) == 4
