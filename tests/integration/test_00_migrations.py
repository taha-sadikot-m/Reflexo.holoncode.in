"""Migrations on a real database: CHK-DB-3 (the whole chain) and CHK-DB-13 (each beta migration with POC data)."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from reflexo.common.db import Database, ServiceReason
from tests.conftest import REPO, PgUrls, alembic

pytestmark = pytest.mark.integration
FIRST_BETA = "0008"


def test_migration_roundtrip(pg: PgUrls) -> None:
    alembic(pg, "downgrade", "base")
    alembic(pg, "upgrade", "head")
    alembic(pg, "current")


def beta_revisions() -> list[tuple[str, str]]:
    """(revision, down_revision) of every beta migration, in chain order."""
    found = []
    for f in sorted(Path(REPO, "migrations", "versions").glob("*.py")):
        source = f.read_text(encoding="utf-8")
        rev = re.search(r'^revision = "(\w+)"', source, re.M)
        down = re.search(r'^down_revision = "(\w+)"', source, re.M)
        if rev and down and rev.group(1) >= FIRST_BETA:
            found.append((rev.group(1), down.group(1)))
    return found


async def poc_counts(db: Database) -> dict[str, int]:
    """Rows of the POC's own data. Platform mirrors are beta rows (made by seeding and the import), not POC data."""
    async with db.service_session(ServiceReason.ADMIN) as s:
        counts = {
            t: int((await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())
            for t in ("businesses", "offerings", "offering_variants", "qa_entries", "orders")
        }
        has_mirrors = (
            await s.execute(
                text("""SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'channel_connections' AND column_name = 'platform_connection_id'""")
            )
        ).first()
        where = " WHERE platform_connection_id IS NULL" if has_mirrors else ""
        counts["channel_connections"] = int(
            (await s.execute(text("SELECT count(*) FROM channel_connections" + where))).scalar_one()
        )
    return counts


async def import_tokens() -> int:
    """The documented step between 0009 and 0010 (scripts/vault_import_env.py)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vault_import_env", REPO / "scripts" / "vault_import_env.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    imported: int = await module.import_tokens()
    return imported


async def migrate(pg: PgUrls, db: Database, *args: str) -> None:
    """Run alembic, then drop pooled connections: they cache enum type ids that a migration may recreate."""
    await asyncio.to_thread(alembic, pg, *args)
    await db.dispose()


async def up_to_head(pg: PgUrls, db: Database, after: str) -> None:
    """Upgrade one migration at a time from `after`, importing the tokens once the vault exists (0009)."""
    for rev, _ in beta_revisions():
        if rev > after:
            await migrate(pg, db, "upgrade", rev)
            if rev == "0009":
                await import_tokens()


@pytest.mark.check("CHK-DB-13")
async def test_each_beta_migration_steps_down_and_up_with_poc_data(pg: PgUrls, db: Database) -> None:
    from reflexo.admin import seeding

    await seeding.reset(db, "full", slugs=("meeras-bakes", "kurti-kraft"))
    async with db.service_session(ServiceReason.ADMIN) as s:  # mirrors are beta rows, made by seeding
        await s.execute(text("DELETE FROM channel_connections WHERE platform_connection_id IS NOT NULL"))
    before = await poc_counts(db)
    assert before["offerings"] > 0 and before["businesses"] >= 2
    revisions = beta_revisions()
    assert revisions and revisions[0][0] == FIRST_BETA
    try:
        for rev, down in reversed(revisions):  # newest first: each one down, then everything back up
            await migrate(pg, db, "downgrade", down)
            assert await poc_counts(db) == before, f"downgrading {rev} lost POC data"
            await up_to_head(pg, db, down)
            assert await poc_counts(db) == before, f"upgrading {rev} lost POC data"
    finally:
        await migrate(pg, db, "upgrade", "head")


@pytest.mark.check("CHK-DB-14")
async def test_0010_refuses_a_real_connection_without_a_credential(pg: PgUrls, db: Database) -> None:
    from reflexo.common.ids import new_id
    from tests.factories import make_tenant

    t = await make_tenant(db, "No credential")
    await migrate(pg, db, "downgrade", "0009")
    conn_id = new_id()
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                                     display_name, credential_ref)
                    VALUES (:id, :b, 'whatsapp', 'meta_whatsapp', :e, 'WA', 'REFLEXO_WA')"""),
            {"id": conn_id, "b": t.business_id, "e": f"pn-{conn_id.hex[-8:]}"},
        )
    try:
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await asyncio.to_thread(alembic, pg, "upgrade", "head")
        assert "vault_import_env" in (refused.value.stderr or "")
    finally:
        async with db.service_session(ServiceReason.ADMIN) as s:
            await s.execute(text("DELETE FROM channel_connections WHERE id = :id"), {"id": conn_id})
        await migrate(pg, db, "upgrade", "head")
