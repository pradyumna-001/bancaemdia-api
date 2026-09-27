"""Issue #90: card_trial_global_catalog."""

import sqlalchemy as sa
from alembic import op

revision = "d90card2026"
down_revision = "c90b1a7e2026"
branch_labels = None
depends_on = None


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


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM assinaturas WHERE trial_confirmed OR provider_customer_ref IS NOT NULL) THEN RAISE EXCEPTION 'billing rollback requires data-preserving migration'; END IF; END $$"
    )
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
