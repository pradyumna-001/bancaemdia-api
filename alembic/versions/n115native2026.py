"""Exact denominated accounts and source bets, isolated from legacy BRL projections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "n115native2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("casa_id", sa.BigInteger(), sa.ForeignKey("casas.id"), nullable=False),
        sa.Column("currency", sa.String(4), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint(
            "id", "usuario_id", "currency", name="uq_native_account_currency_owner"
        ),
        sa.CheckConstraint("currency IN ('BRL','USDT')", name="ck_native_account_currency"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from < valid_to", name="ck_native_account_period"
        ),
    )
    op.create_table(
        "native_bets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("currency", sa.String(4), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("stake", sa.Numeric(50, 30), nullable=False),
        sa.Column("returned", sa.Numeric(50, 30), nullable=False),
        sa.Column("game_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("canonical", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("usuario_id", "identity_hash", name="uq_native_bet_owner_identity"),
        sa.UniqueConstraint("id", "usuario_id", name="uq_native_bet_owner"),
        sa.ForeignKeyConstraint(
            ["account_id", "usuario_id", "currency"],
            ["native_accounts.id", "native_accounts.usuario_id", "native_accounts.currency"],
            name="fk_native_bet_account_currency_owner",
        ),
        sa.CheckConstraint("currency IN ('BRL','USDT')", name="ck_native_bet_currency"),
        sa.CheckConstraint("stake > 0 AND returned >= 0", name="ck_native_bet_amounts"),
        sa.CheckConstraint("state IN ('GREEN','RED')", name="ck_native_bet_state"),
        sa.CheckConstraint("state <> 'RED' OR returned = 0", name="ck_native_bet_loss"),
        sa.CheckConstraint("state <> 'GREEN' OR returned > 0", name="ck_native_bet_win"),
    )
    op.create_table(
        "native_bet_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("bet_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("canonical", JSONB(), nullable=False),
        sa.Column("envelope", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bet_id", "usuario_id"],
            ["native_bets.id", "native_bets.usuario_id"],
            name="fk_native_evidence_bet_owner",
        ),
        sa.UniqueConstraint(
            "usuario_id", "bet_id", "source_hash", name="uq_native_evidence_content"
        ),
    )
    uid = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
    for table in ("native_accounts", "native_bets", "native_bet_evidence"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_read ON {table} FOR SELECT USING (usuario_id = {uid})")
        op.execute(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK (usuario_id = {uid})"
        )
        if table != "native_bet_evidence":
            op.execute(
                f"CREATE POLICY {table}_update ON {table} FOR UPDATE USING (usuario_id = {uid}) WITH CHECK (usuario_id = {uid})"
            )
        op.create_index(f"idx_{table}_owner", table, ["usuario_id"])
        op.execute(
            f"CREATE POLICY {table}_erase ON {table} FOR DELETE USING (usuario_id = {uid} AND usuario_id = NULLIF(current_setting('app.erase_user_data', true), '')::bigint)"
        )
        op.execute(
            f"CREATE TRIGGER active_{table}_write BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
        )
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
        )
        op.execute(
            f"CREATE TRIGGER audit_{table}_write AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
        )
    op.create_index(
        "idx_native_evidence_admission", "native_bet_evidence", ["usuario_id", "created_at"]
    )
    op.create_index("idx_native_bet_game", "native_bets", ["usuario_id", "game_at"])


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM native_accounts) OR EXISTS (SELECT 1 FROM native_bets) OR EXISTS (SELECT 1 FROM native_bet_evidence) THEN RAISE EXCEPTION 'native financial evidence exists: roll back application, retain schema'; END IF; END $$"
    )
    for table in ("native_bet_evidence", "native_bets", "native_accounts"):
        op.drop_table(table)
