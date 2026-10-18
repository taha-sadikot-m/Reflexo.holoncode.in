"""Credential vault and per-business connections (WP-02; BETA_02 §4.1 to §4.3, §7; BETA_10 §4).

- Enums `edition`, `credential_kind`, `connect_flow_status`; `customer_mode` gains `shadow` (used from WP-19).
- `organizations` (service only); `platform_connections` (platform pattern) for the Reflexo number and bot.
- `businesses`, `team_members`, `owner_channel_bindings` changes; `credential_vault` (tenant) and
  `credential_access_log` (append-only); `channel_connections` changes; `connect_flows`;
  `integration_connections.credential_id`.
- `channel_connections.simulated` (beyond BETA_02, fixed there in the same change): demo, sign-up and eval shops
  have WhatsApp-shaped connections whose traffic is all simulated; they need no credential, so the check added in
  0010 exempts them instead of copying a real token into every sandbox.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TENANT = "business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid"
CREDENTIAL_KINDS = (
    "meta_business_token", "meta_page_token", "instagram_user_token", "telegram_bot_token", "webhook_secret",
    "whatsapp_pin", "razorpay_keys", "mcp_oauth", "mcp_static", "store_api", "shipping_api", "google_oauth",
    "email_provider", "slack_bot",
)  # fmt: skip


def _tenant(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_app ON {table} TO reflexo_app USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(f"CREATE POLICY {table}_service ON {table} TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute(f"GRANT {grants} ON {table} TO reflexo_app, reflexo_service")


def upgrade() -> None:
    op.execute("ALTER TYPE customer_mode ADD VALUE IF NOT EXISTS 'shadow'")
    op.execute("CREATE TYPE edition AS ENUM ('owner','team')")
    op.execute(f"CREATE TYPE credential_kind AS ENUM ({', '.join(repr(k) for k in CREDENTIAL_KINDS)})")
    op.execute("""CREATE TYPE connect_flow_status AS ENUM ('started','authorized','completed','failed','expired',
                  'cancelled')""")

    op.execute("""
        CREATE TABLE organizations (
          id uuid PRIMARY KEY,
          name text NOT NULL,
          kind text NOT NULL DEFAULT 'single' CHECK (kind IN ('single','agency')),
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY organizations_service ON organizations TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO reflexo_service")

    op.execute("""
        CREATE TABLE platform_connections (
          id uuid PRIMARY KEY,
          kind text NOT NULL CHECK (kind IN ('reflexo_whatsapp','reflexo_telegram','reflexo_slack')),
          channel channel_type NOT NULL,
          provider channel_provider NOT NULL,
          external_account_id text NOT NULL,
          display_name text NOT NULL,
          status connection_status NOT NULL DEFAULT 'active',
          settings jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (provider, external_account_id)
        )""")
    op.execute("ALTER TABLE platform_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_connections FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY platform_connections_app_read ON platform_connections FOR SELECT TO reflexo_app USING (true)"
    )
    op.execute(
        "CREATE POLICY platform_connections_service ON platform_connections TO reflexo_service USING (true) "
        "WITH CHECK (true)"
    )
    op.execute("GRANT SELECT ON platform_connections TO reflexo_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON platform_connections TO reflexo_service")

    op.execute("""
        ALTER TABLE businesses
          ADD COLUMN edition edition NOT NULL DEFAULT 'owner',
          ADD COLUMN business_type text CHECK (business_type IN
            ('bakery','home_food','clothing','gifting','beauty','jewellery','other')),
          ADD COLUMN organization_id uuid REFERENCES organizations(id) ON DELETE SET NULL,
          ADD COLUMN signup_source text NOT NULL DEFAULT 'web'
            CHECK (signup_source IN ('web','reflexo_whatsapp','reflexo_telegram','admin','seed','demo')),
          ADD COLUMN last_activity_at timestamptz NOT NULL DEFAULT now()""")
    op.execute("ALTER TABLE team_members ADD COLUMN email text")
    op.execute("""
        ALTER TABLE owner_channel_bindings
          ADD COLUMN platform_connection_id uuid REFERENCES platform_connections(id) ON DELETE CASCADE,
          ADD COLUMN notify_preferred boolean NOT NULL DEFAULT false,
          ADD COLUMN last_active_at timestamptz,
          ADD COLUMN verified_via text CHECK (verified_via IN
            ('platform_sender','binding_code','signup_otp','telegram_start','seed'))""")
    op.execute("""CREATE UNIQUE INDEX bindings_one_preferred ON owner_channel_bindings (member_id)
                  WHERE notify_preferred AND revoked_at IS NULL""")

    op.execute("""
        CREATE TABLE credential_vault (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          kind credential_kind NOT NULL,
          label text NOT NULL,
          key_id text NOT NULL,
          nonce bytea NOT NULL,
          ciphertext bytea NOT NULL,
          meta jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','rotated','revoked')),
          rotated_from uuid REFERENCES credential_vault(id) ON DELETE SET NULL,
          last_used_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute(
        "CREATE INDEX credential_vault_refresh ON credential_vault ((meta->>'refresh_after')) WHERE status = 'active'"
    )
    _tenant("credential_vault", "SELECT, INSERT, UPDATE, DELETE")

    op.execute("""
        CREATE TABLE credential_access_log (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          credential_id uuid NOT NULL REFERENCES credential_vault(id) ON DELETE CASCADE,
          purpose text NOT NULL,
          accessor text NOT NULL,
          ok boolean NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX credential_access_by_cred ON credential_access_log (credential_id, created_at)")
    _tenant("credential_access_log", "SELECT, INSERT")  # append-only (CHK-DB-8)

    op.execute("""
        ALTER TABLE channel_connections
          ADD COLUMN credential_id uuid REFERENCES credential_vault(id) ON DELETE SET NULL,
          ADD COLUMN platform_connection_id uuid REFERENCES platform_connections(id) ON DELETE CASCADE,
          ADD COLUMN simulated boolean NOT NULL DEFAULT false,
          ADD COLUMN connect_meta jsonb NOT NULL DEFAULT '{}',
          ADD COLUMN health jsonb NOT NULL DEFAULT '{}',
          ADD COLUMN connected_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
          ADD COLUMN connected_at timestamptz,
          ADD COLUMN disconnected_at timestamptz,
          ALTER COLUMN roles SET DEFAULT '{customer}'""")
    # simulator connections, and WhatsApp-shaped ones on a fake number (sim-wa-*), carry only simulated traffic
    op.execute("""UPDATE channel_connections SET simulated = true
                  WHERE provider = 'simulator' OR external_account_id LIKE 'sim-%'""")
    op.execute(
        "ALTER TABLE channel_connections DROP CONSTRAINT channel_connections_provider_external_account_id_channel_key"
    )
    op.execute("""CREATE UNIQUE INDEX channel_connections_account ON channel_connections
                  (provider, external_account_id, channel) WHERE platform_connection_id IS NULL""")
    op.execute("""CREATE UNIQUE INDEX channel_connections_mirror ON channel_connections
                  (business_id, platform_connection_id) WHERE platform_connection_id IS NOT NULL""")

    op.execute("""
        CREATE TABLE connect_flows (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          target text NOT NULL CHECK (target IN ('channel','connector')),
          provider text NOT NULL CHECK (provider IN ('meta_whatsapp','meta_pages','meta_instagram','telegram','mcp')),
          connector_connection_id uuid,
          started_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
          state_hash bytea NOT NULL UNIQUE,
          sealed bytea,
          status connect_flow_status NOT NULL DEFAULT 'started',
          step text,
          result jsonb NOT NULL DEFAULT '{}',
          error jsonb,
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz
        )""")
    _tenant("connect_flows", "SELECT, INSERT, UPDATE, DELETE")

    op.execute(
        "ALTER TABLE integration_connections ADD COLUMN credential_id uuid "
        "REFERENCES credential_vault(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE integration_connections DROP COLUMN IF EXISTS credential_id")
    op.execute("DROP TABLE IF EXISTS connect_flows")
    op.execute("DROP INDEX IF EXISTS channel_connections_mirror")
    op.execute("DROP INDEX IF EXISTS channel_connections_account")
    op.execute("DELETE FROM channel_connections WHERE platform_connection_id IS NOT NULL")  # mirrors are beta-only
    op.execute("""ALTER TABLE channel_connections
                  ADD CONSTRAINT channel_connections_provider_external_account_id_channel_key
                  UNIQUE (provider, external_account_id, channel)""")
    op.execute("""
        ALTER TABLE channel_connections
          DROP COLUMN credential_id, DROP COLUMN platform_connection_id, DROP COLUMN simulated,
          DROP COLUMN connect_meta, DROP COLUMN health, DROP COLUMN connected_by_member_id,
          DROP COLUMN connected_at, DROP COLUMN disconnected_at,
          ALTER COLUMN roles SET DEFAULT '{customer,owner}'""")
    op.execute("DROP TABLE IF EXISTS credential_access_log")
    op.execute("DROP TABLE IF EXISTS credential_vault")
    op.execute("DROP INDEX IF EXISTS bindings_one_preferred")
    op.execute("""ALTER TABLE owner_channel_bindings DROP COLUMN platform_connection_id, DROP COLUMN notify_preferred,
                  DROP COLUMN last_active_at, DROP COLUMN verified_via""")
    op.execute("ALTER TABLE team_members DROP COLUMN email")
    op.execute("""ALTER TABLE businesses DROP COLUMN edition, DROP COLUMN business_type, DROP COLUMN organization_id,
                  DROP COLUMN signup_source, DROP COLUMN last_activity_at""")
    op.execute("DROP TABLE IF EXISTS platform_connections")
    op.execute("DROP TABLE IF EXISTS organizations")
    op.execute("DROP TYPE IF EXISTS connect_flow_status")
    op.execute("DROP TYPE IF EXISTS credential_kind")
    op.execute("DROP TYPE IF EXISTS edition")
    # Postgres cannot drop one enum value: rebuild customer_mode without 'shadow' (no row may use it before WP-19)
    op.execute("UPDATE channel_connections SET customer_mode = 'tiered' WHERE customer_mode = 'shadow'")
    op.execute("ALTER TYPE customer_mode RENAME TO customer_mode_beta")
    op.execute("CREATE TYPE customer_mode AS ENUM ('tiered','draft_all','off')")
    op.execute("ALTER TABLE channel_connections ALTER COLUMN customer_mode DROP DEFAULT")
    op.execute("""ALTER TABLE channel_connections ALTER COLUMN customer_mode TYPE customer_mode
                  USING customer_mode::text::customer_mode""")
    op.execute("ALTER TABLE channel_connections ALTER COLUMN customer_mode SET DEFAULT 'tiered'")
    op.execute("DROP TYPE customer_mode_beta")
