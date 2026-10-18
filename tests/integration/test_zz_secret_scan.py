"""After the whole run, no table but the vault holds a fixture token, key or PIN (CHK-DB-7; BETA_02 §1).

Runs last (file order): every integration test before it has written connections, sends, events and logs into the
shared test database. Every text and JSON column of every other table is scanned for the fixture secrets.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from reflexo.common.db import Database, ServiceReason
from tests.conftest import TEST_SECRETS

pytestmark = pytest.mark.integration
SECRET_SETTINGS = (
    "REFLEXO_WA_ACCESS_TOKEN",
    "REFLEXO_TELEGRAM_BOT_TOKEN",
    "REFLEXO_META_APP_SECRET",
    "REFLEXO_TELEGRAM_WEBHOOK_SECRET",
    "REFLEXO_SIMULATOR_SECRET",
    "REFLEXO_MEDIA_URL_SECRET",
    "REFLEXO_ADMIN_SESSION_SECRET",
)


def secrets() -> list[str]:
    values = [TEST_SECRETS[k] for k in SECRET_SETTINGS if TEST_SECRETS.get(k)]
    values += [part.split(":", 1)[1] for part in TEST_SECRETS["REFLEXO_VAULT_KEYS"].split(",")]  # the keys
    return values


@pytest.mark.check("CHK-DB-7")
async def test_no_table_but_the_vault_holds_a_fixture_secret(db: Database) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        columns = (
            await s.execute(
                text("""SELECT c.table_name, c.column_name FROM information_schema.columns c
                        JOIN information_schema.tables t ON t.table_name = c.table_name AND t.table_schema = 'public'
                        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
                          AND c.data_type IN ('text', 'character varying', 'jsonb', 'json', 'ARRAY')
                          AND c.table_name NOT IN ('credential_vault', 'alembic_version')""")
            )
        ).all()
        found = []
        patterns = [f"%{v}%" for v in secrets()]
        for col in columns:
            hits = (
                await s.execute(
                    text(f'SELECT count(*) FROM "{col.table_name}" WHERE "{col.column_name}"::text LIKE ANY(:p)'),
                    {"p": patterns},
                )
            ).scalar_one()
            if hits:
                found.append(f"{col.table_name}.{col.column_name}: {hits} row(s)")
    assert len(columns) > 100  # the scan really covers the schema
    assert found == []
