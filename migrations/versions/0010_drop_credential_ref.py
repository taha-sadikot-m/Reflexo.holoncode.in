"""Drop the environment-backed credential references (WP-02; BETA_02 §4.3, §7).

Runs only after `scripts/vault_import_env.py` has moved the showcase seeds' environment tokens into the vault:
it refuses to upgrade while any active connection that is neither simulated nor a platform mirror has no
`credential_id` (CHK-DB-14). Then both `credential_ref` columns go, and every connection must have a credential,
be a platform mirror, or be simulated.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    missing = (
        op.get_bind()
        .execute(
            sa.text("""SELECT count(*) FROM channel_connections
                       WHERE status = 'active' AND NOT simulated AND provider <> 'simulator'
                         AND platform_connection_id IS NULL AND credential_id IS NULL""")
        )
        .scalar_one()
    )
    if missing:
        raise RuntimeError(
            f"{missing} active connection(s) have no vault credential: run scripts/vault_import_env.py first"
        )
    op.execute("ALTER TABLE channel_connections DROP COLUMN credential_ref")
    op.execute("ALTER TABLE integration_connections DROP COLUMN credential_ref")
    op.execute("""ALTER TABLE channel_connections ADD CONSTRAINT channel_connections_has_credential
                  CHECK (platform_connection_id IS NOT NULL OR credential_id IS NOT NULL OR simulated
                         OR provider = 'simulator' OR status <> 'active')""")


def downgrade() -> None:
    op.execute("ALTER TABLE channel_connections DROP CONSTRAINT IF EXISTS channel_connections_has_credential")
    op.execute("ALTER TABLE channel_connections ADD COLUMN credential_ref text")
    # the showcase token group each real connection came from, so a later upgrade can import it again
    op.execute("""UPDATE channel_connections SET credential_ref = CASE provider
                    WHEN 'meta_whatsapp' THEN 'REFLEXO_WA' WHEN 'meta_pages' THEN 'REFLEXO_FB'
                    WHEN 'meta_instagram' THEN 'REFLEXO_IG' WHEN 'telegram' THEN 'REFLEXO_TELEGRAM' END
                  WHERE NOT simulated AND provider <> 'simulator' AND platform_connection_id IS NULL""")
    op.execute("ALTER TABLE integration_connections ADD COLUMN credential_ref text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE integration_connections ALTER COLUMN credential_ref DROP DEFAULT")
