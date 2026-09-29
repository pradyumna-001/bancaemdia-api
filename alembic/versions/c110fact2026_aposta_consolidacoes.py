"""One financial fact, two preserved sources. No destructive rollback with evidence."""

from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c110fact2026"
down_revision = "c109match2026"
branch_labels = None
depends_on = None

EVENT_TYPES = (
    "APOSTA_CRIADA",
    "ODD_ALTERADA",
    "STAKE_ALTERADA",
    "RESULTADO_REGISTRADO",
    "CASHOUT_REGISTRADO",
    "APOSTA_ANULADA",
    "APOSTA_CANCELADA",
    "SELECAO_ALTERADA",
    "CORRECAO_MANUAL",
    "MOVIMENTO_REGISTRADO",
    "CLV_REGISTRADO",
    "REVISAO_RESOLVIDA",
    "APOSTAS_CONSOLIDADAS",
    "CONSOLIDACAO_DESVINCULADA",
    "CONSOLIDACAO_REJEITADA",
)

CANONICAL = """CREATE VIEW public.apostas_financeiras WITH (security_invoker=true) AS
    SELECT a.* FROM public.apostas a WHERE NOT EXISTS (
      SELECT 1 FROM public.aposta_consolidacoes r WHERE r.usuario_id=a.usuario_id
      AND r.telegram_aposta_id=a.id AND r.estado='active')"""


def _dashboard(canonical: bool) -> None:
    # Reuse the immutable published definitions, preserving columns and formula conventions.
    previous = run_path(
        str(Path(__file__).with_name("d3f6a8c1e209_008_painel_materialized_views.py"))
    )
    sql = previous["APOSTAS_METRICAS"].replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)
    summary = previous["MV_RESUMO"]
    prior_balance = Path(__file__).with_name("b71c6a93e402_012_painel_saldo_pre_movimento.py")
    if prior_balance.exists():
        # The converged dependency history has the newer unknown-balance rule and a
        # retained legacy MV. Preserve that rule and avoid reusing its legacy index name.
        summary = run_path(str(prior_balance))["_corrected_summary"]()
    if canonical:
        sql = sql.replace("public.apostas AS a", "public.apostas_financeiras AS a")
        sql = sql.replace(
            "COALESCE(a.tipster_id, 0)",
            "COALESCE(a.tipster_id, (SELECT (r.contexto->>'tipster_id')::bigint FROM public.aposta_consolidacoes r WHERE r.usuario_id=a.usuario_id AND r.casa_aposta_id=a.id AND r.estado='active'), 0)",
        )
        summary = summary.replace("public.apostas AS a", "public.apostas_financeiras AS a")
    op.execute(sql)
    # Only the summary bypasses apostas_metricas for balance. Preserve existing role grants.
    op.execute("""CREATE TEMP TABLE consolidation_dashboard_grants ON COMMIT DROP AS
        SELECT grantee::regrole::text AS role FROM
        aclexplode((SELECT relacl FROM pg_class WHERE oid='public.painel_resumo'::regclass))
        WHERE privilege_type='SELECT' AND grantee<>0""")
    op.execute("DROP VIEW public.painel_resumo")
    op.execute("DROP MATERIALIZED VIEW painel.mv_painel_resumo")
    op.execute(summary)
    op.execute(
        "CREATE UNIQUE INDEX uq_mv_resumo_consolidacao ON painel.mv_painel_resumo (usuario_id)"
    )
    op.execute("""CREATE VIEW public.painel_resumo WITH (security_barrier=true) AS
      SELECT fonte.* FROM painel.mv_painel_resumo fonte WHERE fonte.usuario_id=
      NULLIF(current_setting('app.current_user_id',true),'')::bigint""")
    op.execute("""DO $$ DECLARE entry record; BEGIN
      FOR entry IN SELECT role FROM consolidation_dashboard_grants LOOP
        EXECUTE format('GRANT SELECT ON public.painel_resumo TO %I',entry.role);
      END LOOP; END $$""")
    op.execute("DROP TABLE consolidation_dashboard_grants")
    for name in previous["MATERIALIZED_VIEWS"]:
        op.execute(f"REFRESH MATERIALIZED VIEW painel.{name}")
    op.execute("UPDATE painel.estado_refresh SET atualizado_em=clock_timestamp() WHERE id=1")


def upgrade() -> None:
    op.create_unique_constraint("uq_contas_consolidacao_owner", "contas_casa", ["id", "usuario_id"])
    op.create_table(
        "aposta_consolidacoes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("casa_aposta_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_aposta_id", sa.BigInteger(), nullable=False),
        sa.Column("conta_casa_id", sa.BigInteger()),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("decisao", sa.String(16), nullable=False),
        sa.Column("versao", sa.String(40), nullable=False),
        sa.Column("evidencia", JSONB(), nullable=False),
        sa.Column("contexto", JSONB(), nullable=False),
        sa.Column("ator", sa.String(40), nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("desvinculada_em", sa.DateTime(timezone=True)),
        sa.Column("motivo_desvinculacao", sa.String()),
        sa.ForeignKeyConstraint(
            ["casa_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        sa.ForeignKeyConstraint(
            ["telegram_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        sa.ForeignKeyConstraint(
            ["conta_casa_id", "usuario_id"], ["contas_casa.id", "contas_casa.usuario_id"]
        ),
        sa.CheckConstraint("casa_aposta_id<>telegram_aposta_id", name="ck_consolidacao_pontas"),
        sa.CheckConstraint(
            "estado IN ('active','unlinked','rejected')", name="ck_consolidacao_estado"
        ),
        sa.CheckConstraint(
            "decisao IN ('automatic','reviewed','legacy')", name="ck_consolidacao_decisao"
        ),
        sa.CheckConstraint(
            "decisao='legacy' OR estado='rejected' OR conta_casa_id IS NOT NULL",
            name="ck_consolidacao_conta",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidencia)='object' AND jsonb_typeof(contexto)='object'",
            name="ck_consolidacao_snapshot",
        ),
        sa.CheckConstraint(
            "(estado='active' AND desvinculada_em IS NULL) OR (estado IN ('unlinked','rejected') AND desvinculada_em IS NOT NULL)",
            name="ck_consolidacao_desvinculacao",
        ),
    )
    for side in ("casa", "telegram"):
        op.create_index(
            f"uq_consolidacao_{side}_ativa",
            "aposta_consolidacoes",
            [f"{side}_aposta_id"],
            unique=True,
            postgresql_where=sa.text("estado='active'"),
        )
    op.create_index(
        "ix_consolidacao_historico",
        "aposta_consolidacoes",
        ["usuario_id", "casa_aposta_id", "telegram_aposta_id"],
    )
    # Adopt reciprocal legacy pairs without inventing matching/automatic evidence. Existing
    # selection remains untouched until a reviewed unlink; contradictory pairs fail upgrade.
    op.execute("""INSERT INTO aposta_consolidacoes
      (usuario_id,casa_aposta_id,telegram_aposta_id,conta_casa_id,estado,decisao,versao,evidencia,contexto,ator)
      SELECT c.usuario_id,c.id,t.id,c.conta_casa_id,'active','legacy','legacy/manual',
        jsonb_build_object('legacy',true,'casa_chave',c.chave,'telegram_chave',t.chave,
          'casa_selecionada',c.selecionada,'telegram_selecionada',t.selecionada),
        jsonb_build_object('tipster_id',c.tipster_id,'tipster_casa_id',c.tipster_id,'chat_id',t.chat_id,'message_id',t.message_id,'midia_hash',t.midia_hash),'migration'
      FROM apostas c JOIN apostas t ON t.usuario_id=c.usuario_id AND t.chave=c.parceira_chave
      WHERE c.origem='casa' AND t.origem IN ('telegram','print') AND t.parceira_chave=c.chave""")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM apostas a WHERE a.parceira_chave IS NOT NULL
      AND NOT EXISTS(SELECT 1 FROM aposta_consolidacoes r WHERE r.usuario_id=a.usuario_id
      AND (r.casa_aposta_id=a.id OR r.telegram_aposta_id=a.id)))
      THEN RAISE EXCEPTION 'legacy pairing requires explicit reconciliation before upgrade'; END IF; END $$""")
    op.execute("ALTER TABLE aposta_consolidacoes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE aposta_consolidacoes FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY consolidacao_por_usuario ON aposta_consolidacoes FOR ALL
      USING(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
      WITH CHECK(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)""")
    op.execute("""CREATE FUNCTION consolidation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'preserve consolidation audit'; END IF;
        IF (to_jsonb(OLD)-ARRAY['estado','desvinculada_em','motivo_desvinculacao']) IS DISTINCT FROM
           (to_jsonb(NEW)-ARRAY['estado','desvinculada_em','motivo_desvinculacao']) OR
           OLD.estado<>'active' OR NEW.estado<>'unlinked' THEN
          RAISE EXCEPTION 'immutable consolidation decision';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER consolidation_immutable BEFORE UPDATE OR DELETE ON aposta_consolidacoes
      FOR EACH ROW EXECUTE FUNCTION consolidation_immutable()""")
    op.execute("""CREATE FUNCTION consolidation_sources() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('pareador:'||NEW.usuario_id,0));
        IF NOT EXISTS(SELECT 1 FROM apostas WHERE id=NEW.casa_aposta_id AND usuario_id=NEW.usuario_id AND origem='casa')
          OR NOT EXISTS(SELECT 1 FROM apostas WHERE id=NEW.telegram_aposta_id AND usuario_id=NEW.usuario_id AND origem IN ('telegram','print'))
        THEN RAISE EXCEPTION 'invalid consolidation sources'; END IF;
        RETURN NEW;
      END $$""")
    op.execute(
        "CREATE TRIGGER consolidation_sources BEFORE INSERT ON aposta_consolidacoes FOR EACH ROW EXECUTE FUNCTION consolidation_sources()"
    )
    op.execute(CANONICAL)
    # Convention follows the append-only event names already published in this repository.
    op.drop_constraint("ck_eventos_tipo", "eventos", type_="check")
    op.create_check_constraint(
        "ck_eventos_tipo", "eventos", f"tipo IN ({','.join(repr(t) for t in EVENT_TYPES)})"
    )
    op.execute("""INSERT INTO eventos(usuario_id,tipo,fonte,aposta_chave,payload_json)
      SELECT r.usuario_id,'APOSTAS_CONSOLIDADAS','manual',a.chave,
        jsonb_build_object('contrato',1,'relacao_id',r.id,'usuario_id',r.usuario_id,
          'casa_aposta_id',r.casa_aposta_id,'telegram_aposta_id',r.telegram_aposta_id,
          'conta_casa_id',r.conta_casa_id,'decisao',r.decisao,'versao',r.versao,
          'evidencia',r.evidencia,'contexto',r.contexto,'ator',r.ator,'criada_em',r.criada_em)
      FROM aposta_consolidacoes r JOIN apostas a
        ON a.usuario_id=r.usuario_id AND a.id IN (r.casa_aposta_id,r.telegram_aposta_id)
      WHERE r.decisao='legacy'""")
    op.execute("""DO $$ DECLARE entry record; BEGIN
      FOR entry IN SELECT DISTINCT grantee::regrole::text AS role FROM
        aclexplode((SELECT relacl FROM pg_class WHERE oid='public.apostas'::regclass))
        WHERE privilege_type='SELECT' AND grantee<>0 LOOP
        EXECUTE format('GRANT SELECT ON public.apostas_financeiras TO %I',entry.role);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON public.aposta_consolidacoes TO %I',entry.role);
        EXECUTE format('GRANT USAGE,SELECT ON SEQUENCE public.aposta_consolidacoes_id_seq TO %I',entry.role);
      END LOOP; END $$""")
    _dashboard(True)


def downgrade() -> None:
    op.execute("SET LOCAL row_security=off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM aposta_consolidacoes) OR EXISTS(
      SELECT 1 FROM eventos WHERE tipo IN ('APOSTAS_CONSOLIDADAS','CONSOLIDACAO_DESVINCULADA','CONSOLIDACAO_REJEITADA'))
      THEN RAISE EXCEPTION 'preserve consolidation evidence before downgrade'; END IF; END $$""")
    _dashboard(False)
    op.execute("DROP VIEW public.apostas_financeiras")
    op.execute("DROP TABLE aposta_consolidacoes")
    op.execute("DROP FUNCTION consolidation_immutable()")
    op.execute("DROP FUNCTION consolidation_sources()")
    op.drop_constraint("uq_contas_consolidacao_owner", "contas_casa", type_="unique")
    op.drop_constraint("ck_eventos_tipo", "eventos", type_="check")
    op.create_check_constraint(
        "ck_eventos_tipo", "eventos", f"tipo IN ({','.join(repr(t) for t in EVENT_TYPES[:-3])})"
    )
