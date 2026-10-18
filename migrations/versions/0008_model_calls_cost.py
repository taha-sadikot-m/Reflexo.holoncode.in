"""Model-call tracing and cost (WP-01, BETA_02 §4.7, BETA_12).

- `model_calls`: one append-only row per model, embedding, transcription or voice-usage block, with its cost in
  micro-rupees at the day's rate (`fx_rate`) and the price-file version, so history is never re-priced.
- `cost_daily`: rollup rebuilt from `model_calls`; `cost_caps`: the daily cap per business and when its 80% alert
  last fired; `fx_rates`: the daily USD->INR rate (platform table); `benchmark_runs`: service only.

Two values beyond BETA_02 §4.7, fixed there in the same change: kind `voice_session` (the LiveKit agent-session
minute of BETA_12 §6.3 had no kind) and feature `messages` (BETA_13 §6.1 writes it).

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
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
    op.execute("""
        CREATE TYPE model_call_kind AS ENUM ('chat','structured','vision','embed','transcribe','voice_stt',
          'voice_llm','voice_tts','voice_session')""")

    op.execute("""
        CREATE TABLE model_calls (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          turn_id uuid,
          run_ref text,
          feature text NOT NULL CHECK (feature IN ('chat','voice','automation_build','automation_run',
            'transcription','embedding','import','screens','report','setup','messages','other')),
          kind model_call_kind NOT NULL,
          purpose text NOT NULL,
          role text,
          provider text NOT NULL,
          model text NOT NULL,
          prompt_version text,
          input_tokens int NOT NULL DEFAULT 0,
          cached_input_tokens int NOT NULL DEFAULT 0,
          output_tokens int NOT NULL DEFAULT 0,
          audio_seconds numeric(10,2) NOT NULL DEFAULT 0,
          characters int NOT NULL DEFAULT 0,
          cost_micro_inr bigint NOT NULL,
          price_version text NOT NULL,
          fx_rate numeric(10,4) NOT NULL DEFAULT 0,
          duration_ms int NOT NULL,
          ttft_ms int,
          hedged boolean NOT NULL DEFAULT false,
          status text NOT NULL CHECK (status IN ('ok','error','timeout')),
          error_code text,
          trace_id text,
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX model_calls_business_time ON model_calls (business_id, created_at)")
    op.execute("CREATE INDEX model_calls_turn ON model_calls (turn_id) WHERE turn_id IS NOT NULL")
    _tenant("model_calls", "SELECT, INSERT")  # append-only: no UPDATE or DELETE grant (CHK-DB-8)

    op.execute("""
        CREATE TABLE cost_daily (
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          day date NOT NULL,
          feature text NOT NULL,
          calls int NOT NULL,
          input_tokens bigint NOT NULL,
          cached_input_tokens bigint NOT NULL,
          output_tokens bigint NOT NULL,
          audio_seconds numeric(12,2) NOT NULL,
          cost_micro_inr bigint NOT NULL,
          PRIMARY KEY (business_id, day, feature)
        )""")
    _tenant("cost_daily", "SELECT, INSERT, UPDATE, DELETE")

    op.execute("""
        CREATE TABLE cost_caps (
          business_id uuid PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
          daily_cap_micro_inr bigint NOT NULL,
          alert_at_pct int NOT NULL DEFAULT 80 CHECK (alert_at_pct BETWEEN 1 AND 99),
          degrade_at_pct int NOT NULL DEFAULT 100,
          pause_at_pct int NOT NULL DEFAULT 150,
          alerted_on date,
          updated_at timestamptz NOT NULL DEFAULT now()
        )""")
    _tenant("cost_caps", "SELECT, INSERT, UPDATE, DELETE")

    op.execute("""
        CREATE TABLE fx_rates (
          day date PRIMARY KEY,
          usd_inr numeric(10,4) NOT NULL,
          source text NOT NULL,
          fetched_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("ALTER TABLE fx_rates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE fx_rates FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY fx_rates_app_read ON fx_rates FOR SELECT TO reflexo_app USING (true)")
    op.execute("CREATE POLICY fx_rates_service ON fx_rates TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute("GRANT SELECT ON fx_rates TO reflexo_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON fx_rates TO reflexo_service")
    # the first known rate (BETA_12 §6.2: Frankfurter, 16 Oct 2026), so costs exist before the first refresh runs
    op.execute("INSERT INTO fx_rates (day, usd_inr, source) VALUES ('2026-10-16', 94.96, 'frankfurter')")

    op.execute("""
        CREATE TABLE benchmark_runs (
          id uuid PRIMARY KEY,
          git_sha text NOT NULL,
          set_version text NOT NULL,
          started_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz,
          results jsonb,
          is_baseline boolean NOT NULL DEFAULT false,
          compared_to uuid REFERENCES benchmark_runs(id) ON DELETE SET NULL,
          passed boolean
        )""")
    op.execute("ALTER TABLE benchmark_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE benchmark_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY benchmark_runs_service ON benchmark_runs TO reflexo_service USING (true) WITH CHECK (true)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON benchmark_runs TO reflexo_service")


def downgrade() -> None:
    for table in ("benchmark_runs", "fx_rates", "cost_caps", "cost_daily", "model_calls"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP TYPE IF EXISTS model_call_kind")
