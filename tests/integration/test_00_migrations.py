"""Migrations on a real database: CHK-DB-3 (the whole chain) and CHK-DB-13 (each beta migration with POC data)."""

from __future__ import annotations

import asyncio
import re
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
    async with db.service_session(ServiceReason.ADMIN) as s:
        return {
            t: int((await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())
            for t in ("businesses", "offerings", "offering_variants", "qa_entries", "channel_connections", "orders")
        }


@pytest.mark.check("CHK-DB-13")
async def test_each_beta_migration_steps_down_and_up_with_poc_data(pg: PgUrls, db: Database) -> None:
    from reflexo.admin import seeding

    await seeding.reset(db, "full", slugs=("meeras-bakes", "kurti-kraft"))
    before = await poc_counts(db)
    assert before["offerings"] > 0 and before["businesses"] >= 2
    revisions = beta_revisions()
    assert revisions and revisions[0][0] == FIRST_BETA
    for rev, down in revisions:
        await asyncio.to_thread(alembic, pg, "downgrade", down)
        assert await poc_counts(db) == before, f"downgrading {rev} lost POC data"
        await asyncio.to_thread(alembic, pg, "upgrade", rev)
        assert await poc_counts(db) == before, f"upgrading {rev} lost POC data"
    await asyncio.to_thread(alembic, pg, "upgrade", "head")
