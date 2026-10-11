"""worker_heartbeats: liveness and pool stats per worker process (admin health, POC_07 §2 Overview).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE worker_heartbeats (
          worker_id text PRIMARY KEY,
          service text NOT NULL,
          pools text[] NOT NULL DEFAULT '{}',
          started_at timestamptz NOT NULL DEFAULT now(),
          beat_at timestamptz NOT NULL DEFAULT now(),
          stats jsonb NOT NULL DEFAULT '{}'
        )""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO reflexo_service")
    op.execute("ALTER TABLE worker_heartbeats ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE worker_heartbeats FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY worker_heartbeats_service ON worker_heartbeats TO reflexo_service USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
