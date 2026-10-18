"""Compiled Cedar policy sets (WP-26; BETA_02 §4.16, BETA_04 §7.4).

One row per business, rule layer and version: the owner's customer-scoped and shop-wide playbook rules compiled
to Cedar, rebuilt in the same transaction as every rule change (change sets, undo included). Superseded versions
are kept: they are the basis of past decisions. The platform, default and ceiling layers are files in
`config/policy/`, not rows. Named `0023_policy_sets` in the planning table; numbered here in build order.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TENANT = "business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE policy_sets (
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          layer text NOT NULL CHECK (layer IN ('customer','shop')),
          version int NOT NULL,
          cedar_text text NOT NULL,
          schema_fragment text NOT NULL,
          source_hash bytea NOT NULL,
          analysis jsonb,
          compiled_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (business_id, layer, version)
        )""")
    op.execute("ALTER TABLE policy_sets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE policy_sets FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY policy_sets_app ON policy_sets TO reflexo_app USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute("CREATE POLICY policy_sets_service ON policy_sets TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute("GRANT SELECT, INSERT, UPDATE ON policy_sets TO reflexo_app, reflexo_service")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS policy_sets")
