"""inbox_events.run_after: retry not-before time (keeps received_at as the true receive time).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE inbox_events ADD COLUMN run_after timestamptz NOT NULL DEFAULT now()")
    op.execute("DROP INDEX IF EXISTS inbox_due")
    op.execute("CREATE INDEX inbox_due ON inbox_events (run_after) WHERE status = 'pending'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS inbox_due")
    op.execute("ALTER TABLE inbox_events DROP COLUMN run_after")
    op.execute("CREATE INDEX inbox_due ON inbox_events (received_at) WHERE status = 'pending'")
