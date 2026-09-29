"""Issue #91: stripe_checkout."""

import sqlalchemy as sa
from alembic import op

revision = "d91checkout2026"
down_revision = "d90card2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM assinaturas WHERE trial_confirmed OR provider_customer_ref IS NOT NULL) THEN RAISE EXCEPTION 'billing rollback requires data-preserving migration'; END IF; END $$"
    )
    op.drop_table("billing_checkouts")
