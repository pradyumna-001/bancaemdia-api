"""Site-managed bankrolls: archival state and idempotent write receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b211banca2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
TABLE = "bancas_requisicoes"


def upgrade() -> None:
    op.add_column("bancas", sa.Column("arquivada_em", sa.DateTime(timezone=True)))
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("operacao", sa.String(16), nullable=False),
        sa.Column("banca_id", sa.BigInteger()),
        sa.Column("chave", sa.String(200), nullable=False),
        sa.Column("pedido_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("resposta", JSONB(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "operacao IN ('CRIAR','ALTERAR','ARQUIVAR')", name="ck_bancas_requisicoes_operacao"
        ),
        sa.UniqueConstraint("usuario_id", "chave", name="uq_bancas_requisicoes_chave"),
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
    # Recibo é escrita comercial como a banca que ele descreve: READ_ONLY não grava nenhum dos dois.
    op.execute(
        f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM bancas_requisicoes)"
        " OR EXISTS (SELECT 1 FROM bancas WHERE arquivada_em IS NOT NULL)"
        " THEN RAISE EXCEPTION 'site bankroll writes exist: roll back application, retain schema';"
        " END IF; END $$"
    )
    op.execute(f"DROP TRIGGER billing_write_guard ON {TABLE}")
    op.execute(f"DROP TRIGGER active_{TABLE}_write ON {TABLE}")
    op.execute(f"DROP TRIGGER audit_{TABLE}_write ON {TABLE}")
    op.execute(f"DROP POLICY {TABLE}_por_usuario ON {TABLE}")
    op.drop_table(TABLE)
    op.drop_column("bancas", "arquivada_em")
