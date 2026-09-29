"""Durable collection submissions and immutable installation session boundaries."""

import sqlalchemy as sa
from alembic import op

revision = "c108v22026"
down_revision = "c107pair2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_coleta_instalacao_identity", "coleta_instalacoes", ["id", "usuario_id"]
    )
    op.add_column("coletas_casa", sa.Column("v2_fonte_em", sa.DateTime(timezone=True)))
    op.add_column("coletas_casa", sa.Column("v2_hash", sa.String(64)))
    op.create_table(
        "coleta_sessoes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sessao_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("instalacao_id", sa.BigInteger(), nullable=False),
        sa.Column("coletar_desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("encerrada_em", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "instalacao_id", "usuario_id", name="uq_coleta_sessao_identity"),
        sa.ForeignKeyConstraint(
            ["instalacao_id", "usuario_id"],
            ["coleta_instalacoes.id", "coleta_instalacoes.usuario_id"],
            name="fk_coleta_sessao_installation",
        ),
    )
    op.create_index(
        "uq_coleta_sessao_open",
        "coleta_sessoes",
        ["instalacao_id"],
        unique=True,
        postgresql_where=sa.text("encerrada_em IS NULL"),
    )
    op.create_table(
        "coleta_entregas",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("job_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("instalacao_id", sa.BigInteger(), nullable=False),
        sa.Column("sessao_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("client_event_id", sa.UUID(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("envelope", sa.dialects.postgresql.JSON()),
        sa.Column("ack", sa.String(16), nullable=False),
        sa.Column("ack_reason", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("aposta_chave", sa.String()),
        sa.Column("tentativas", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finalizada_em", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("instalacao_id", "client_event_id", name="uq_coleta_entrega_event"),
        sa.ForeignKeyConstraint(
            ["sessao_id", "instalacao_id", "usuario_id"],
            ["coleta_sessoes.id", "coleta_sessoes.instalacao_id", "coleta_sessoes.usuario_id"],
            name="fk_coleta_entrega_session",
        ),
    )
    op.create_index("ix_coleta_entregas_pending", "coleta_entregas", ["status", "id"])
    for table in ("coleta_sessoes", "coleta_entregas"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_owner ON {table} FOR ALL
            USING(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
            WITH CHECK(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)""")
    op.execute("""CREATE FUNCTION coleta_session_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF NEW.coletar_desde IS DISTINCT FROM OLD.coletar_desde OR
           NEW.instalacao_id IS DISTINCT FROM OLD.instalacao_id OR
           NEW.usuario_id IS DISTINCT FROM OLD.usuario_id OR NEW.sessao_id IS DISTINCT FROM OLD.sessao_id OR
           (OLD.encerrada_em IS NOT NULL AND NEW.encerrada_em IS DISTINCT FROM OLD.encerrada_em)
        THEN RAISE EXCEPTION 'collection session identity and boundary are immutable'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER coleta_session_immutable BEFORE UPDATE ON coleta_sessoes
        FOR EACH ROW EXECUTE FUNCTION coleta_session_immutable()""")
    # The privileged migration owner only exposes bounded routing metadata, never raw captures.
    op.execute("""CREATE FUNCTION coleta_pending_deliveries(maximum integer)
        RETURNS TABLE(usuario_id bigint, job_id uuid) LANGUAGE sql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        SELECT usuario_id,job_id FROM public.coleta_entregas WHERE status='pending'
        ORDER BY id LIMIT LEAST(GREATEST(maximum,0),500) $$""")
    op.execute("REVOKE ALL ON FUNCTION coleta_pending_deliveries(integer) FROM PUBLIC")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
        GRANT EXECUTE ON FUNCTION coleta_pending_deliveries(integer) TO bancaemdia_app;
        END IF; END $$""")


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM coleta_entregas) OR EXISTS(SELECT 1 FROM coleta_sessoes)
        THEN RAISE EXCEPTION 'preserve collection sessions and delivery inbox before downgrade'; END IF; END $$""")
    op.execute("DROP FUNCTION coleta_pending_deliveries(integer)")
    op.drop_table("coleta_entregas")
    op.drop_table("coleta_sessoes")
    op.execute("DROP FUNCTION coleta_session_immutable()")
    op.drop_column("coletas_casa", "v2_hash")
    op.drop_column("coletas_casa", "v2_fonte_em")
    op.drop_constraint("uq_coleta_instalacao_identity", "coleta_instalacoes", type_="unique")
