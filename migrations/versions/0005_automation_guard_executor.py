"""automation_executor gains 'guard': guards run synchronously inside order confirmation (POC_06 §3, §6).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE automation_executor ADD VALUE IF NOT EXISTS 'guard'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; guard versions are re-labelled so a re-upgrade is clean.
    op.execute("UPDATE automation_versions SET executor = 'instant' WHERE executor = 'guard'")
