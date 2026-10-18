"""Schema guarantees: FORCE RLS everywhere (CHK-DB-1), enum mirror (CHK-DB-2), tenant isolation (CHK-F12-1)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reflexo.common.db import Database, ServiceReason
from reflexo.common.enums import PG_ENUMS
from reflexo.common.ids import new_id
from tests.factories import make_tenant

pytestmark = pytest.mark.integration


@pytest.mark.check("CHK-ARCH-7")
async def test_every_table_has_forced_rls(db: Database) -> None:
    async with db.service_session(ServiceReason.HEALTH) as s:
        rows = (
            await s.execute(
                text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'""")
            )
        ).all()
    assert len(rows) == 66  # 52 baseline + worker_heartbeats (0003) + playbook_rules (0006) + owner_accounts (0007)
    # + model_calls, cost_daily, cost_caps, fx_rates, benchmark_runs (0008)
    # + organizations, platform_connections, credential_vault, credential_access_log, connect_flows (0009)
    # + policy_sets (0011)
    missing = [r.relname for r in rows if not (r.relrowsecurity and r.relforcerowsecurity)]
    assert missing == [], f"tables without forced RLS: {missing}"


@pytest.mark.check("CHK-ARCH-7")
async def test_tenant_tables_have_app_policy(db: Database) -> None:
    async with db.service_session(ServiceReason.HEALTH) as s:
        missing = (
            (
                await s.execute(
                    text("""
            SELECT ic.table_name FROM information_schema.columns ic
            WHERE ic.table_schema = 'public' AND ic.column_name = 'business_id'
              AND NOT EXISTS (SELECT 1 FROM pg_policies p WHERE p.tablename = ic.table_name
                              AND p.policyname = ic.table_name || '_app')""")
                )
            )
            .scalars()
            .all()
        )
    assert missing == []


async def test_postgres_enums_match_python(db: Database) -> None:
    async with db.service_session(ServiceReason.HEALTH) as s:
        rows = (
            await s.execute(
                text("""
            SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
            FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public'
            GROUP BY t.typname""")
            )
        ).all()
    db_enums = {r.typname: list(r.labels) for r in rows}
    assert set(db_enums) == set(PG_ENUMS), set(db_enums) ^ set(PG_ENUMS)
    for name, py_enum in PG_ENUMS.items():
        assert db_enums[name] == [m.value for m in py_enum], name


async def test_cross_business_read_and_write_blocked(db: Database) -> None:
    a = await make_tenant(db, "A")
    b = await make_tenant(db, "B")
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO offerings (id, business_id, name) VALUES (:id, :b, 'B cake')"),
            {"id": new_id(), "b": b.business_id},
        )

    async with db.tenant_session(a.business_id) as s:
        count = (
            await s.execute(text("SELECT count(*) FROM offerings WHERE business_id = :b"), {"b": b.business_id})
        ).scalar_one()
        assert count == 0
        visible_businesses = (await s.execute(text("SELECT id FROM businesses"))).scalars().all()
        assert visible_businesses == [a.business_id]

    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant_session(a.business_id) as s:
            await s.execute(
                text("INSERT INTO offerings (id, business_id, name) VALUES (:id, :b, 'x')"),
                {"id": new_id(), "b": b.business_id},
            )


async def test_app_role_without_business_context_sees_nothing(db: Database) -> None:
    await make_tenant(db, "C")
    async with db._app_sessions() as session, session.begin():  # deliberately no set_config
        assert (await session.execute(text("SELECT count(*) FROM customers"))).scalar_one() == 0


async def test_platform_tables_invisible_to_app_role(db: Database) -> None:
    t = await make_tenant(db, "D")
    async with db.service_session(ServiceReason.WEBHOOK_INGRESS) as s:
        await s.execute(
            text("""INSERT INTO inbox_events (id, provider, dedupe_key, headers, payload)
                                VALUES (:id, 'simulator', :k, '{}', '{}')"""),
            {"id": new_id(), "k": str(new_id())},
        )
    async with db.tenant_session(t.business_id) as s:
        assert (await s.execute(text("SELECT count(*) FROM inbox_events"))).scalar_one() == 0
