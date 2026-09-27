"""Stripe inbox, card-confirmed trial and database write enforcement.

Revision ID: d91a92b932026
Revises: c90b1a7e2026
"""

import sqlalchemy as sa
from alembic import op

revision = "d91a92b932026"
down_revision = "c90b1a7e2026"
branch_labels = None
depends_on = None

WRITE_TABLES = (
    "bancas",
    "contas_casa",
    "unidades",
    "movimentos",
    "movimento_requisicoes",
    "apostas",
    "eventos",
    "revisao_pendente",
    "coletas_casa",
    "coleta_token",
    "chamadas_ia",
    "uploads",
    "upload_bilhetes",
    "upload_arquivos",
)


def upgrade() -> None:
    op.drop_constraint("ex_billing_price_published_overlap", "billing_prices")
    op.drop_constraint("ck_billing_price_currency", "billing_prices")
    op.create_check_constraint(
        "ck_billing_price_currency", "billing_prices", "currency ~ '^[A-Z]{3}$'"
    )
    op.execute(
        "ALTER TABLE billing_prices ADD CONSTRAINT ex_billing_price_published_overlap EXCLUDE USING gist (product WITH =, currency WITH =, frequency WITH =, tstzrange(valid_from, valid_until, '[)') WITH &&) WHERE (published)"
    )
    op.add_column(
        "assinaturas",
        sa.Column("trial_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "assinaturas",
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_unique_constraint("uq_billing_customer", "assinaturas", ["provider_customer_ref"])
    op.create_unique_constraint(
        "uq_billing_subscription", "assinaturas", ["provider_subscription_ref"]
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION billing_trial_guard() RETURNS trigger
        LANGUAGE plpgsql AS $billing$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'trial grant is permanent'; END IF;
            IF NEW.usuario_id <> OLD.usuario_id OR (OLD.trial_confirmed AND NOT NEW.trial_confirmed) THEN
                RAISE EXCEPTION 'trial identity is permanent';
            END IF;
            IF (NEW.trial_started_at IS DISTINCT FROM OLD.trial_started_at OR
                NEW.trial_ends_at IS DISTINCT FROM OLD.trial_ends_at) AND
                NOT (NOT OLD.trial_confirmed AND NEW.trial_confirmed) THEN
                RAISE EXCEPTION 'trial grant is immutable';
            END IF;
            IF NEW.status = 'TRIALING' AND OLD.status <> 'TRIALING' AND OLD.trial_confirmed THEN
                RAISE EXCEPTION 'a second trial is forbidden';
            END IF;
            RETURN NEW;
        END $billing$
    """)
    op.create_table(
        "billing_checkouts",
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), primary_key=True),
        sa.Column("operation", sa.String(), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("price_id", sa.BigInteger(), sa.ForeignKey("billing_prices.id"), nullable=False),
        sa.Column("trial", sa.Boolean(), nullable=False),
        sa.Column("session_ref", sa.String()),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("ALTER TABLE billing_checkouts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_checkouts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY billing_checkouts_tenant ON billing_checkouts USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint) WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)"
    )
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
    op.execute("""
        CREATE FUNCTION billing_require_write() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE uid bigint; t timestamptz; allowed boolean; grant_row record;
        BEGIN
            uid := COALESCE(to_jsonb(NEW)->>'usuario_id', to_jsonb(OLD)->>'usuario_id')::bigint;
            IF uid IS NULL OR NOT EXISTS (SELECT 1 FROM public.billing_rollout WHERE activated_at IS NOT NULL) THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
            END IF;
            SELECT * INTO grant_row FROM public.assinaturas WHERE usuario_id=uid FOR SHARE;
            t := clock_timestamp();
            allowed := (grant_row.trial_confirmed AND grant_row.trial_started_at <= t AND t < grant_row.trial_ends_at)
                OR (grant_row.status='ACTIVE' AND grant_row.current_period_started_at <= t AND t < grant_row.current_period_ends_at);
            IF allowed IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'account_read_only' USING ERRCODE='P0402';
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
        END $billing$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_require_write() FROM PUBLIC")
    for table in WRITE_TABLES:
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
        )


def downgrade() -> None:
    # Refuse destructive rollback once any Stripe identity or confirmed trial exists.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM assinaturas WHERE trial_confirmed OR provider_customer_ref IS NOT NULL) THEN RAISE EXCEPTION 'billing rollback requires data-preserving migration'; END IF; END $$"
    )
    for table in WRITE_TABLES:
        op.execute(f"DROP TRIGGER billing_write_guard ON {table}")
    op.execute("DROP FUNCTION billing_require_write()")
    op.execute("DROP FUNCTION billing_accounts_to_reconcile()")
    op.execute("DROP POLICY billing_reconcile_owner ON assinaturas")
    op.drop_table("billing_events")
    op.drop_table("billing_checkouts")
    op.execute("DROP TRIGGER billing_trial_immutable ON assinaturas")
    op.execute("DROP FUNCTION billing_trial_guard()")
    op.execute(
        "CREATE FUNCTION billing_trial_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF TG_OP='DELETE' THEN RAISE EXCEPTION 'trial grant is permanent'; END IF; IF NEW.usuario_id <> OLD.usuario_id OR NEW.trial_started_at IS DISTINCT FROM OLD.trial_started_at OR NEW.trial_ends_at IS DISTINCT FROM OLD.trial_ends_at OR (NEW.status='TRIALING' AND OLD.status<>'TRIALING') THEN RAISE EXCEPTION 'trial grant is immutable'; END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER billing_trial_immutable BEFORE UPDATE OR DELETE ON assinaturas FOR EACH ROW EXECUTE FUNCTION billing_trial_guard()"
    )
    op.drop_constraint("uq_billing_customer", "assinaturas")
    op.drop_constraint("uq_billing_subscription", "assinaturas")
    op.drop_column("assinaturas", "cancel_at_period_end")
    op.drop_column("assinaturas", "trial_confirmed")
    op.drop_constraint("ex_billing_price_published_overlap", "billing_prices")
    op.drop_constraint("ck_billing_price_currency", "billing_prices")
    op.create_check_constraint("ck_billing_price_currency", "billing_prices", "currency = 'BRL'")
    op.execute(
        "ALTER TABLE billing_prices ADD CONSTRAINT ex_billing_price_published_overlap EXCLUDE USING gist (product WITH =, tstzrange(valid_from, valid_until, '[)') WITH &&) WHERE (published)"
    )
