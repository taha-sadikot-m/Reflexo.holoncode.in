"""Owner accounts for self-serve sign-up (the public site's /signup and /login).

One row per owner login: email + password hash (scrypt), the business and team member it signs in as. Looked up
only by the API's auth code through a service session; owners never read this table through the app role.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE owner_accounts (
          id uuid PRIMARY KEY,
          email text NOT NULL,                      -- stored lower-cased
          password_hash text NOT NULL,              -- scrypt$n$r$p$salt$hash (hex)
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          member_id uuid NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now(),
          last_login_at timestamptz,
          CHECK (email = lower(email))
        )""")
    op.execute("CREATE UNIQUE INDEX owner_accounts_email ON owner_accounts (email)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON owner_accounts TO reflexo_service")
    op.execute("ALTER TABLE owner_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_accounts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY owner_accounts_service ON owner_accounts TO reflexo_service USING (true) WITH CHECK (true)"
    )
    # every tenant table carries the tenant policy (schema invariant); the app role has no grant here at all, so
    # password hashes stay out of reach of tenant sessions either way
    op.execute(
        "CREATE POLICY owner_accounts_app ON owner_accounts TO reflexo_app "
        "USING (business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS owner_accounts")
