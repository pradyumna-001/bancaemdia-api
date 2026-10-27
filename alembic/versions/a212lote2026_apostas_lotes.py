"""Reversible batch deletion of bets: one receipt per intention, the exact keys it touched."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "a212lote2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
TABLE = "apostas_lotes"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("publico_id", UUID(as_uuid=True), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("operacao", sa.String(16), nullable=False),
        sa.Column("lote_id", UUID(as_uuid=True)),
        sa.Column("chave", sa.String(200), nullable=False),
        sa.Column("pedido_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("resposta", JSONB(), nullable=False),
        sa.Column("chaves", JSONB()),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(operacao = 'APAGAR' AND lote_id IS NULL AND chaves IS NOT NULL)"
            " OR (operacao = 'RESTAURAR' AND lote_id IS NOT NULL AND chaves IS NULL)",
            name="ck_apostas_lotes_operacao",
        ),
        sa.UniqueConstraint("usuario_id", "chave", name="uq_apostas_lotes_chave"),
        sa.UniqueConstraint("publico_id", name="uq_apostas_lotes_publico"),
    )
    op.create_index(
        "uq_apostas_lotes_restauracao",
        TABLE,
        ["lote_id"],
        unique=True,
        postgresql_where=sa.text("operacao = 'RESTAURAR'"),
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_por_usuario ON {TABLE} FOR ALL "
        f"USING (usuario_id = {TENANT}) WITH CHECK (usuario_id = {TENANT})"
    )
    op.execute(
        f"CREATE TRIGGER audit_{TABLE}_write AFTER INSERT OR UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
    )
    op.execute(
        f"CREATE TRIGGER active_{TABLE}_write BEFORE INSERT OR UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
    )
    # O recibo é parte da mesma escrita comercial das apostas: READ_ONLY não grava nenhum.
    op.execute(
        f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM apostas_lotes) THEN RAISE EXCEPTION"
        " 'batch deletion receipts exist: roll back application, retain schema'; END IF; END $$"
    )
    op.execute(f"DROP TRIGGER billing_write_guard ON {TABLE}")
    op.execute(f"DROP TRIGGER active_{TABLE}_write ON {TABLE}")
    op.execute(f"DROP TRIGGER audit_{TABLE}_write ON {TABLE}")
    op.execute(f"DROP POLICY {TABLE}_por_usuario ON {TABLE}")
    op.drop_index("uq_apostas_lotes_restauracao", table_name=TABLE)
    op.drop_table(TABLE)
