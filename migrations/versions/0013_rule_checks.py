"""The rule checker's last result on each rule (WP-26, stage 2 audit; BETA_02 §4.11).

`playbook_rules.check_result` holds `RuleCheckRecord`: ok, unchecked (saved as "ask me first" until the hourly
re-check passes) or blocked, the effect the owner asked for, and what the rule newly allows or forbids. The Rules page
shows it (BETA_13 §2.1). Rules saved before this migration have null.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE playbook_rules ADD COLUMN check_result jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE playbook_rules DROP COLUMN IF EXISTS check_result")
