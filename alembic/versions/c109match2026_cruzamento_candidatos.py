"""Versioned non-financial matching candidates and indexed source snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c109match2026"
down_revision = "a9d6e3f1c210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_apostas_matching_owner", "apostas", ["id", "usuario_id"])
    op.create_table(
        "cruzamento_entradas",
        sa.Column("aposta_id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("casa", sa.String()),
        sa.Column("origem", sa.String(), nullable=False),
        sa.Column("ocorrido_em", sa.DateTime(timezone=True)),
        sa.Column("dados", JSONB(), nullable=False),
        sa.Column(
            "atualizada_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_cruzamento_busca",
        "cruzamento_entradas",
        ["usuario_id", "casa", "origem", "ocorrido_em", "aposta_id"],
    )
    op.create_table(
        "cruzamento_candidatos",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("casa_aposta_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_aposta_id", sa.BigInteger(), nullable=False),
        sa.Column("versao", sa.String(40), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("classe_base", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("busca_truncada", sa.Boolean(), nullable=False),
        sa.Column("sinais_iguais", JSONB(), nullable=False),
        sa.Column("sinais_conflitantes", JSONB(), nullable=False),
        sa.Column("evidencia", JSONB(), nullable=False),
        sa.Column("explicacao", sa.String(), nullable=False),
        sa.Column("revisao_id", sa.BigInteger(), sa.ForeignKey("revisao_pendente.id")),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["casa_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        sa.ForeignKeyConstraint(
            ["telegram_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        sa.UniqueConstraint(
            "casa_aposta_id", "telegram_aposta_id", "versao", name="uq_cruzamento_par_versao"
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_cruzamento_score"),
        sa.CheckConstraint(
            "status IN ('exact','probable','incompatible','excluded')", name="ck_cruzamento_status"
        ),
    )
    for side in ("casa", "telegram"):
        op.create_index(
            f"ix_cruzamento_{side}",
            "cruzamento_candidatos",
            ["usuario_id", f"{side}_aposta_id", "status"],
        )
    for table in ("cruzamento_entradas", "cruzamento_candidatos"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_por_usuario ON {table} FOR ALL
            USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
            WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)""")

    op.execute("""CREATE FUNCTION matching_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('pareador:'||NEW.usuario_id,0));
        UPDATE revisao_pendente SET resolvido_em=now() WHERE usuario_id=NEW.usuario_id AND id IN
          (SELECT revisao_id FROM cruzamento_candidatos WHERE usuario_id=NEW.usuario_id
           AND (casa_aposta_id=NEW.id OR telegram_aposta_id=NEW.id) AND status<>'excluded');
        UPDATE cruzamento_candidatos SET status='excluded',atualizado_em=now()
          WHERE usuario_id=NEW.usuario_id AND (casa_aposta_id=NEW.id OR telegram_aposta_id=NEW.id)
          AND status<>'excluded';
        DELETE FROM cruzamento_entradas WHERE usuario_id=NEW.usuario_id AND aposta_id=NEW.id;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER matching_source_changed AFTER UPDATE ON apostas
        FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION matching_source_changed()""")


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM cruzamento_candidatos)
        THEN RAISE EXCEPTION 'preserve matching evidence before downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER matching_source_changed ON apostas")
    op.execute("DROP FUNCTION matching_source_changed()")
    op.drop_table("cruzamento_candidatos")
    op.drop_table("cruzamento_entradas")
    op.drop_constraint("uq_apostas_matching_owner", "apostas", type_="unique")
