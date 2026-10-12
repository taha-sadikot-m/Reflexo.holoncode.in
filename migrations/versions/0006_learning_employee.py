"""Learning employee (ADR-015, POC_10): playbook rules the owner teaches, order cancellation facts.

- `playbook_rules`: when the agent may / may not do a capability, or must propose it to the owner. Conditions are a
  typed list (field, op, value) so they evaluate deterministically and read back in plain words. Created and undone
  through knowledge change sets (entity type `playbook_rule`).
- `orders`: who cancelled, when and why.
- Proposals reuse `approval_requests` (kind `tool_call`); no table needed.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ENTITY_TYPES_OLD = "'offering','variant','qa','profile','detail_schema'"
ENTITY_TYPES_NEW = ENTITY_TYPES_OLD + ",'playbook_rule'"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE playbook_rules (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          capability text NOT NULL,                 -- 'order.cancel', 'order.change', ...
          effect text NOT NULL CHECK (effect IN ('allow', 'deny', 'propose')),
          conditions jsonb NOT NULL DEFAULT '[]',   -- model: list[Condition] {field, op, value}; [] = always
          says text NOT NULL,                       -- the owner's words (or the decision it was learned from)
          scope answer_scope NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'customer')),
          customer_id uuid REFERENCES customers(id) ON DELETE CASCADE,
          source text NOT NULL CHECK (source IN ('owner_instruction', 'owner_decision')),
          source_ref uuid,                          -- approval_requests.id of the deciding proposal
          status record_status NOT NULL DEFAULT 'active',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK ((scope = 'customer') = (customer_id IS NOT NULL))
        )""")
    op.execute("CREATE INDEX playbook_rules_active ON playbook_rules (business_id, capability) WHERE status = 'active'")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON playbook_rules TO reflexo_app, reflexo_service")
    op.execute("ALTER TABLE playbook_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE playbook_rules FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY playbook_rules_service ON playbook_rules TO reflexo_service USING (true) WITH CHECK (true)")
    op.execute(
        "CREATE POLICY playbook_rules_app ON playbook_rules TO reflexo_app "
        "USING (business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid) "
        "WITH CHECK (business_id = NULLIF(current_setting('app.current_business_id', true), '')::uuid)"
    )
    op.execute("ALTER TABLE knowledge_change_items DROP CONSTRAINT knowledge_change_items_entity_type_check")
    op.execute(
        "ALTER TABLE knowledge_change_items ADD CONSTRAINT knowledge_change_items_entity_type_check "
        f"CHECK (entity_type IN ({ENTITY_TYPES_NEW}))"
    )
    op.execute("ALTER TABLE orders ADD COLUMN cancelled_at timestamptz")
    op.execute("ALTER TABLE orders ADD COLUMN cancelled_by text")  # 'customer' | 'owner' | 'system'
    op.execute("ALTER TABLE orders ADD COLUMN cancel_reason text")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS cancel_reason")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS cancelled_by")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS cancelled_at")
    op.execute("DELETE FROM knowledge_change_items WHERE entity_type = 'playbook_rule'")
    op.execute("ALTER TABLE knowledge_change_items DROP CONSTRAINT knowledge_change_items_entity_type_check")
    op.execute(
        "ALTER TABLE knowledge_change_items ADD CONSTRAINT knowledge_change_items_entity_type_check "
        f"CHECK (entity_type IN ({ENTITY_TYPES_OLD}))"
    )
    op.execute("DROP TABLE IF EXISTS playbook_rules")
