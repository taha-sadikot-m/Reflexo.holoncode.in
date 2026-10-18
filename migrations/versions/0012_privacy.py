"""Privacy basics (WP-22a; BETA_02 §4.13, BETA_10 §6, §7).

`consent_records` and `data_requests` (tenant tables), `operator_access_log` (append-only: every operator view of a
business's customer data) and `customers.notice_shown_at` (the first AI reply carried the privacy notice). Named
`0021_privacy` in the planning table; numbered here in build order. `archive_objects`, `archive_access_log` and
`processing_log` come later, with the work packages that first need them.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TENANT = "business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid"


def _tenant(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_app ON {table} TO reflexo_app USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(f"CREATE POLICY {table}_service ON {table} TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute(f"GRANT {grants} ON {table} TO reflexo_app, reflexo_service")


def upgrade() -> None:
    op.execute("CREATE TYPE data_request_kind AS ENUM ('export','deletion','correction')")
    op.execute("CREATE TYPE data_request_status AS ENUM ('received','verifying','in_progress','completed','rejected')")
    op.execute("""
        CREATE TABLE consent_records (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          subject_type text NOT NULL CHECK (subject_type IN ('owner','customer','staff')),
          subject_id uuid NOT NULL,
          purpose text NOT NULL CHECK (purpose IN ('service_terms','history_import','cross_business_learning',
            'marketing_broadcast','call_permission','voice_note_eval')),
          granted boolean NOT NULL,
          method text NOT NULL CHECK (method IN ('signup_terms','in_chat','meta_flow','whatsapp_optin',
            'owner_on_behalf')),
          evidence jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(),
          withdrawn_at timestamptz
        )""")
    op.execute("CREATE INDEX consent_records_subject ON consent_records (business_id, subject_id, purpose)")
    _tenant("consent_records", "SELECT, INSERT, UPDATE")
    op.execute("""
        CREATE TABLE data_requests (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          subject_type text NOT NULL CHECK (subject_type IN ('owner','customer','staff')),
          subject_id uuid NOT NULL,
          kind data_request_kind NOT NULL,
          status data_request_status NOT NULL DEFAULT 'received',
          requested_via channel_type,
          requested_at timestamptz NOT NULL DEFAULT now(),
          due_at timestamptz NOT NULL,
          completed_at timestamptz,
          result_media_object_id uuid REFERENCES media_objects(id) ON DELETE SET NULL,
          notes text
        )""")
    op.execute("CREATE INDEX data_requests_open ON data_requests (business_id, due_at) WHERE completed_at IS NULL")
    _tenant("data_requests", "SELECT, INSERT, UPDATE")
    op.execute("""
        CREATE TABLE operator_access_log (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          operator text NOT NULL,
          object_type text NOT NULL,
          object_id uuid,
          reason text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX operator_access_log_business ON operator_access_log (business_id, created_at DESC)")
    _tenant("operator_access_log", "SELECT, INSERT")  # append-only
    op.execute("ALTER TABLE customers ADD COLUMN notice_shown_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS notice_shown_at")
    op.execute("DROP TABLE IF EXISTS operator_access_log")
    op.execute("DROP TABLE IF EXISTS data_requests")
    op.execute("DROP TABLE IF EXISTS consent_records")
    op.execute("DROP TYPE IF EXISTS data_request_status")
    op.execute("DROP TYPE IF EXISTS data_request_kind")
