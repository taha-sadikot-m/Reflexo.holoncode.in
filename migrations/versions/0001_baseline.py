"""Baseline schema (POC_02): tables, enums, privileges, row-level security.

Revision ID: 0001
Revises:
"""

from pathlib import Path

from alembic import op
from sqlalchemy.util import await_only

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parent.parent / "sql"


def _exec_file(name: str) -> None:
    """Run a multi-statement SQL script.

    SQLAlchemy's asyncpg adapter prepares statements, and prepared statements accept one command only.
    The raw asyncpg `execute()` without arguments uses the simple query protocol, which runs whole scripts
    (including DO blocks) inside the migration's transaction. `await_only` bridges into it from Alembic's
    greenlet context.
    """
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute((_SQL / name).read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_file("0001_baseline_up.sql")


def downgrade() -> None:
    _exec_file("0001_baseline_down.sql")
