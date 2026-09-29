"""Provider-neutral billing foundation; rollout deliberately remains inactive.

Revision ID: c90b1a7e2026
Revises: b4e2a7d9c143
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ExcludeConstraint

revision: str = "c90b1a7e2026"
down_revision: str | Sequence[str] | None = "b4e2a7d9c143"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "billing_rollout",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_billing_rollout_singleton"),
    )
    op.execute("INSERT INTO billing_rollout (id) VALUES (1)")
    op.execute("ALTER TABLE billing_rollout ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY billing_rollout_read ON billing_rollout FOR SELECT USING (true)")
    op.create_table(
        "billing_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("product", sa.String(), server_default=sa.text("'bancaemdia'"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("frequency", sa.String(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("provider_plan_ref", sa.String()),
        sa.Column("published", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("product = 'bancaemdia'", name="ck_billing_price_product"),
        sa.CheckConstraint("amount_cents > 0", name="ck_billing_price_amount"),
        sa.CheckConstraint("currency = 'BRL'", name="ck_billing_price_currency"),
        sa.CheckConstraint("frequency IN ('MONTHLY', 'YEARLY')", name="ck_billing_price_frequency"),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_billing_price_dates"
        ),
        sa.CheckConstraint(
            "NOT published OR published_at IS NOT NULL", name="ck_billing_price_publication"
        ),
        ExcludeConstraint(
            ("product", "="),
            (sa.text("tstzrange(valid_from, valid_until, '[)')"), "&&"),
            name="ex_billing_price_published_overlap",
            where=sa.text("published"),
            using="gist",
        ),
    )
    op.create_index("idx_billing_price_product_from", "billing_prices", ["product", "valid_from"])
    op.execute("ALTER TABLE billing_prices ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY billing_prices_read ON billing_prices FOR SELECT USING (true)")
    op.create_table(
        "billing_price_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("price_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("fields", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute("ALTER TABLE billing_price_audit ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY billing_price_audit_read ON billing_price_audit FOR SELECT USING (true)"
    )
    op.execute("""
        CREATE TRIGGER billing_price_audit_no_change
        BEFORE UPDATE OR DELETE OR TRUNCATE ON billing_price_audit
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()
    """)
    op.execute("""
        CREATE FUNCTION billing_price_audit_write() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE changed jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'price versions are permanent';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.published_at IS NOT NULL AND (
                NEW.amount_cents IS DISTINCT FROM OLD.amount_cents OR
                NEW.currency IS DISTINCT FROM OLD.currency OR
                NEW.frequency IS DISTINCT FROM OLD.frequency OR
                NEW.valid_from IS DISTINCT FROM OLD.valid_from OR
                NEW.provider_plan_ref IS DISTINCT FROM OLD.provider_plan_ref
            ) THEN
                RAISE EXCEPTION 'published price terms are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.published_at IS DISTINCT FROM OLD.published_at THEN
                RAISE EXCEPTION 'publication timestamp is immutable';
            END IF;
            IF NEW.published AND NEW.published_at IS NULL THEN
                NEW.published_at := clock_timestamp();
            END IF;
            IF TG_OP = 'INSERT' THEN
                SELECT COALESCE(jsonb_agg(field ORDER BY field), '[]'::jsonb)
                  INTO changed FROM jsonb_object_keys(to_jsonb(NEW)) AS keys(field);
            ELSE
                SELECT COALESCE(jsonb_agg(field ORDER BY field), '[]'::jsonb)
                  INTO changed FROM jsonb_object_keys(to_jsonb(NEW)) AS keys(field)
                 WHERE to_jsonb(OLD)->field IS DISTINCT FROM to_jsonb(NEW)->field;
            END IF;
            INSERT INTO public.billing_price_audit (price_id, action, fields)
            VALUES (NEW.id, TG_OP, changed);
            RETURN NEW;
        END
        $billing$
    """)
    op.execute("""
        CREATE TRIGGER audit_billing_prices_write BEFORE INSERT OR UPDATE OR DELETE ON billing_prices
        FOR EACH ROW EXECUTE FUNCTION billing_price_audit_write()
    """)
    op.create_table(
        "assinaturas",
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_started_at", sa.DateTime(timezone=True)),
        sa.Column("current_period_ends_at", sa.DateTime(timezone=True)),
        sa.Column("price_id", sa.BigInteger(), sa.ForeignKey("billing_prices.id")),
        sa.Column("provider", sa.String()),
        sa.Column("provider_customer_ref", sa.String()),
        sa.Column("provider_subscription_ref", sa.String()),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELED', 'EXPIRED')",
            name="ck_assinatura_status",
        ),
        sa.CheckConstraint(
            "trial_ends_at = trial_started_at + interval '7 days'",
            name="ck_assinatura_trial_length",
        ),
        sa.CheckConstraint(
            "(current_period_started_at IS NULL AND current_period_ends_at IS NULL) "
            "OR (current_period_started_at IS NOT NULL AND current_period_ends_at > current_period_started_at)",
            name="ck_assinatura_period",
        ),
        sa.CheckConstraint(
            "status <> 'ACTIVE' OR (price_id IS NOT NULL AND current_period_started_at IS NOT NULL)",
            name="ck_assinatura_active_terms",
        ),
    )
    op.create_index("idx_assinaturas_status", "assinaturas", ["status"])
    op.execute("ALTER TABLE assinaturas ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assinaturas FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY assinaturas_por_usuario ON assinaturas FOR ALL
        USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
        WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
    """)
    # Only the user-creation trigger may insert before a request has a tenant identity.
    op.execute("""
        CREATE POLICY assinaturas_cadastro ON assinaturas FOR INSERT
        WITH CHECK (pg_trigger_depth() > 0 AND EXISTS (
            SELECT 1 FROM usuarios WHERE id = usuario_id
        ))
    """)
    op.execute("""
        CREATE POLICY assinaturas_backfill ON assinaturas FOR INSERT
        WITH CHECK (current_user = (
            SELECT rolname FROM pg_roles WHERE oid = (
                SELECT relowner FROM pg_class WHERE oid = 'public.assinaturas'::regclass
            )
        ))
    """)
    op.execute("""
        CREATE FUNCTION billing_new_user_trial() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE started timestamptz;
                grant_at timestamptz;
        BEGIN
            SELECT activated_at INTO started FROM public.billing_rollout WHERE id = 1 FOR SHARE;
            IF started IS NOT NULL AND NEW.ativo THEN
                grant_at := clock_timestamp();
                INSERT INTO public.assinaturas
                    (usuario_id, status, trial_started_at, trial_ends_at)
                VALUES (NEW.id, 'TRIALING', grant_at, grant_at + interval '7 days')
                ON CONFLICT (usuario_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END
        $billing$
    """)
    op.execute("""
        CREATE TRIGGER billing_usuario_created AFTER INSERT ON usuarios
        FOR EACH ROW EXECUTE FUNCTION billing_new_user_trial()
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_new_user_trial() FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION billing_activate_rollout() RETURNS timestamptz
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE started timestamptz;
        BEGIN
            UPDATE public.billing_rollout SET activated_at = clock_timestamp()
            WHERE id = 1 AND activated_at IS NULL RETURNING activated_at INTO started;
            IF started IS NULL THEN
                SELECT activated_at INTO started FROM public.billing_rollout WHERE id = 1;
            END IF;
            INSERT INTO public.assinaturas
                (usuario_id, status, trial_started_at, trial_ends_at)
            SELECT u.id, 'TRIALING', started, started + interval '7 days'
            FROM public.usuarios u WHERE u.ativo
            ON CONFLICT (usuario_id) DO NOTHING;
            RETURN started;
        END
        $billing$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_activate_rollout() FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION billing_trial_guard() RETURNS trigger
        LANGUAGE plpgsql AS $billing$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'trial grant is permanent';
            END IF;
            IF NEW.usuario_id <> OLD.usuario_id OR
               NEW.trial_started_at IS DISTINCT FROM OLD.trial_started_at OR
               NEW.trial_ends_at IS DISTINCT FROM OLD.trial_ends_at THEN
                RAISE EXCEPTION 'trial grant is immutable';
            END IF;
            IF NEW.status = 'TRIALING' AND OLD.status <> 'TRIALING' THEN
                RAISE EXCEPTION 'a second trial is forbidden';
            END IF;
            RETURN NEW;
        END
        $billing$
    """)
    op.execute("""
        CREATE TRIGGER billing_trial_immutable BEFORE UPDATE OR DELETE ON assinaturas
        FOR EACH ROW EXECUTE FUNCTION billing_trial_guard()
    """)
    op.execute("""
        CREATE TRIGGER audit_assinaturas_write AFTER INSERT OR UPDATE OR DELETE ON assinaturas
        FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()
    """)
    op.execute("""
        CREATE TRIGGER active_assinaturas_write BEFORE INSERT OR UPDATE ON assinaturas
        FOR EACH ROW EXECUTE FUNCTION require_active_tenant()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER active_assinaturas_write ON assinaturas")
    op.execute("DROP TRIGGER audit_assinaturas_write ON assinaturas")
    op.execute("DROP TRIGGER billing_trial_immutable ON assinaturas")
    op.execute("DROP FUNCTION billing_trial_guard()")
    op.execute("DROP TRIGGER billing_usuario_created ON usuarios")
    op.execute("DROP FUNCTION billing_new_user_trial()")
    op.execute("DROP FUNCTION billing_activate_rollout()")
    op.execute("DROP POLICY assinaturas_cadastro ON assinaturas")
    op.execute("DROP POLICY assinaturas_backfill ON assinaturas")
    op.execute("DROP POLICY assinaturas_por_usuario ON assinaturas")
    op.drop_index("idx_assinaturas_status", table_name="assinaturas")
    op.drop_table("assinaturas")
    op.execute("DROP POLICY billing_prices_read ON billing_prices")
    op.execute("DROP TRIGGER audit_billing_prices_write ON billing_prices")
    op.execute("DROP FUNCTION billing_price_audit_write()")
    op.execute("DROP TRIGGER billing_price_audit_no_change ON billing_price_audit")
    op.execute("DROP POLICY billing_price_audit_read ON billing_price_audit")
    op.drop_table("billing_price_audit")
    op.drop_index("idx_billing_price_product_from", table_name="billing_prices")
    op.drop_table("billing_prices")
    op.execute("DROP POLICY billing_rollout_read ON billing_rollout")
    op.drop_table("billing_rollout")
