"""Stable holders, account usage intervals, and audited holder switches.

Revision ID: d94a7b1c2026
Revises: c90b1a7e2026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d94a7b1c2026"
down_revision: str | Sequence[str] | None = "c90b1a7e2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
TABLES = ("titulares", "usos_conta_casa", "trocas_titular_requisicoes", "trocas_titular_eventos")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "titulares",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("nome", sa.String(160), nullable=False),
        sa.Column("arquivado", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(btrim(nome)) BETWEEN 1 AND 160", name="ck_titulares_nome"),
        sa.UniqueConstraint("usuario_id", "id", name="uq_titulares_usuario_id"),
    )
    op.create_index("idx_titulares_usuario_nome", "titulares", ["usuario_id", "nome"])
    op.add_column("contas_casa", sa.Column("titular_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "contas_casa",
        sa.Column("estado", sa.String(), server_default=sa.text("'DISPONIVEL'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_contas_casa_estado",
        "contas_casa",
        "estado IN ('DISPONIVEL','EM_USO','LIMITADA','ENCERRADA')",
    )
    op.create_unique_constraint("uq_contas_casa_usuario_id", "contas_casa", ["usuario_id", "id"])
    op.create_unique_constraint(
        "uq_contas_casa_usuario_casa_id", "contas_casa", ["usuario_id", "casa_id", "id"]
    )
    op.create_foreign_key(
        "fk_contas_casa_titular_usuario",
        "contas_casa",
        "titulares",
        ["usuario_id", "titular_id"],
        ["usuario_id", "id"],
    )
    op.create_index("idx_contas_casa_titular", "contas_casa", ["usuario_id", "titular_id"])
    op.create_index(
        "uq_contas_casa_titular_casa",
        "contas_casa",
        ["usuario_id", "casa_id", "titular_id"],
        unique=True,
        postgresql_where=sa.text("titular_id IS NOT NULL"),
    )
    op.create_table(
        "usos_conta_casa",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("casa_id", sa.BigInteger(), nullable=False),
        sa.Column("conta_casa_id", sa.BigInteger(), nullable=False),
        # NULL lower bound means the old account's start is unknown, not an invented date.
        sa.Column("vigente_de", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vigente_ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origem", sa.String(), server_default=sa.text("'EXPLICITA'"), nullable=False),
        sa.CheckConstraint(
            "vigente_de IS NULL OR vigente_ate IS NULL OR vigente_de < vigente_ate",
            name="ck_usos_conta_casa_intervalo",
        ),
        sa.CheckConstraint("origem IN ('LEGADO','EXPLICITA')", name="ck_usos_conta_casa_origem"),
        sa.CheckConstraint(
            "origem = 'LEGADO' OR vigente_de IS NOT NULL",
            name="ck_usos_conta_casa_inicio_explicito",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id", "casa_id", "conta_casa_id"],
            ["contas_casa.usuario_id", "contas_casa.casa_id", "contas_casa.id"],
            name="fk_usos_conta_casa_identidade",
        ),
    )
    op.create_index(
        "idx_usos_conta_casa_tempo", "usos_conta_casa", ["usuario_id", "casa_id", "vigente_de"]
    )
    op.create_index(
        "uq_usos_conta_casa_um_aberto_v1",
        "usos_conta_casa",
        ["usuario_id", "casa_id"],
        unique=True,
        postgresql_where=sa.text("vigente_ate IS NULL"),
    )
    op.execute("""
        ALTER TABLE usos_conta_casa ADD CONSTRAINT ex_usos_conta_casa_sem_sobreposicao_v1
        EXCLUDE USING gist
        (usuario_id WITH =, casa_id WITH =, tstzrange(vigente_de, vigente_ate, '[)') WITH &&)
    """)
    # Existing references remain untouched. Backfill only a unique unbounded active account.
    # Ambiguous or closed legacy rows need user review and never gain a guessed usage.
    op.execute("ALTER TABLE contas_casa NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        INSERT INTO usos_conta_casa (usuario_id, casa_id, conta_casa_id, vigente_de, origem)
        SELECT c.usuario_id, c.casa_id, c.id, c.desde, 'LEGADO'
          FROM contas_casa c
         WHERE c.ativa AND c.ate IS NULL
           AND (c.desde IS NULL OR c.desde <= clock_timestamp())
           AND 1 = (SELECT count(*) FROM contas_casa other
                     WHERE other.usuario_id = c.usuario_id AND other.casa_id = c.casa_id
                       AND other.ativa AND other.ate IS NULL)
    """)
    op.execute("""
        UPDATE contas_casa c SET estado = 'EM_USO'
         WHERE EXISTS (SELECT 1 FROM usos_conta_casa u WHERE u.conta_casa_id = c.id)
    """)
    op.execute("ALTER TABLE contas_casa FORCE ROW LEVEL SECURITY")
    op.create_table(
        "trocas_titular_requisicoes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("chave", sa.String(160), nullable=False),
        sa.Column("pedido_hash", sa.String(64), nullable=False),
        sa.Column("resposta", JSONB(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("tipo IN ('PREVIEW','APPLY')", name="ck_trocas_titular_tipo"),
        sa.UniqueConstraint("usuario_id", "tipo", "chave", name="uq_trocas_titular_idempotencia"),
    )
    op.create_index(
        "idx_trocas_titular_requisicoes_usuario",
        "trocas_titular_requisicoes",
        ["usuario_id", "criado_em"],
    )
    op.create_table(
        "trocas_titular_eventos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("requisicao_id", sa.BigInteger(), nullable=False),
        sa.Column("dados", JSONB(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "idx_trocas_titular_eventos_usuario",
        "trocas_titular_eventos",
        ["usuario_id", "criado_em"],
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_por_usuario ON {table} FOR ALL "
            f"USING (usuario_id = {TENANT}) WITH CHECK (usuario_id = {TENANT})"
        )
        op.execute(
            f"CREATE TRIGGER audit_{table}_write AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
        )
        op.execute(
            f"CREATE TRIGGER active_{table}_write BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
        )
    op.execute("""
        CREATE TRIGGER trocas_titular_eventos_no_change
        BEFORE UPDATE OR DELETE OR TRUNCATE ON trocas_titular_eventos
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trocas_titular_eventos_no_change ON trocas_titular_eventos")
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER active_{table}_write ON {table}")
        op.execute(f"DROP TRIGGER audit_{table}_write ON {table}")
        op.execute(f"DROP POLICY {table}_por_usuario ON {table}")
    op.drop_table("trocas_titular_eventos")
    op.drop_table("trocas_titular_requisicoes")
    op.drop_table("usos_conta_casa")
    op.drop_index("idx_contas_casa_titular", table_name="contas_casa")
    op.drop_index("uq_contas_casa_titular_casa", table_name="contas_casa")
    op.drop_constraint("fk_contas_casa_titular_usuario", "contas_casa", type_="foreignkey")
    op.drop_constraint("uq_contas_casa_usuario_casa_id", "contas_casa", type_="unique")
    op.drop_constraint("uq_contas_casa_usuario_id", "contas_casa", type_="unique")
    op.drop_constraint("ck_contas_casa_estado", "contas_casa", type_="check")
    op.drop_column("contas_casa", "estado")
    op.drop_column("contas_casa", "titular_id")
    op.drop_table("titulares")
