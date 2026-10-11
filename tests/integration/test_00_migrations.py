"""CHK-DB-3: migrations upgrade → downgrade → upgrade on a real database."""

from __future__ import annotations

import pytest

from tests.conftest import PgUrls, alembic

pytestmark = pytest.mark.integration


def test_migration_roundtrip(pg: PgUrls) -> None:
    alembic(pg, "downgrade", "base")
    alembic(pg, "upgrade", "head")
    alembic(pg, "current")
