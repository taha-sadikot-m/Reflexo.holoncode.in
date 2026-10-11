"""call_sessions: one session per provider call id (Meta retries `connect`; the bridge must act once).

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX call_sessions_provider_call ON call_sessions (provider_call_id) "
        "WHERE provider_call_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX call_sessions_live ON call_sessions (bridge_host) WHERE status IN ('connecting', 'active')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS call_sessions_live")
    op.execute("DROP INDEX IF EXISTS call_sessions_provider_call")
