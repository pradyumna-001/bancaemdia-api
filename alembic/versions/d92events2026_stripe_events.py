"""Issue #92: stripe_events."""

import sqlalchemy as sa
from alembic import op

revision = "d92events2026"
down_revision = "d91checkout2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("customer_ref", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_error", sa.String()),
    )
    op.create_index("idx_billing_events_pending", "billing_events", ["state", "next_attempt_at"])
    # The inbox contains no tenant payload. Its worker/webhook policies are separate
    # from user-scoped business tables; no HTTP read/update surface exposes it.
    op.execute("ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY billing_events_service ON billing_events USING (true) WITH CHECK (true)"
    )
    op.execute("""
        CREATE FUNCTION billing_accounts_to_reconcile() RETURNS TABLE (usuario_id bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
            SELECT a.usuario_id FROM public.assinaturas a JOIN public.usuarios u ON u.id=a.usuario_id
            WHERE u.ativo AND a.provider_customer_ref IS NOT NULL
            ORDER BY a.last_reconciled_at ASC NULLS FIRST, a.usuario_id LIMIT 100
        $billing$
    """)
    # FORCE RLS applies even to the migration owner: narrowly scoped read for this
    # SECURITY DEFINER discovery function, with no provider/customer data returned.
    op.execute(
        "CREATE POLICY billing_reconcile_owner ON assinaturas FOR SELECT USING (current_user = (SELECT rolname FROM pg_roles WHERE oid=(SELECT relowner FROM pg_class WHERE oid='public.assinaturas'::regclass)))"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM assinaturas WHERE trial_confirmed OR provider_customer_ref IS NOT NULL) THEN RAISE EXCEPTION 'billing rollback requires data-preserving migration'; END IF; END $$"
    )
    op.execute("DROP FUNCTION billing_accounts_to_reconcile()")
    op.execute("DROP POLICY billing_reconcile_owner ON assinaturas")
    op.drop_table("billing_events")
