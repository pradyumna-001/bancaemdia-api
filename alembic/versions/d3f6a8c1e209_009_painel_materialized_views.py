"""009_painel_materialized_views

Revision ID: d3f6a8c1e209
Revises: f2a9c4e7b106
Create Date: 2026-09-21 21:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3f6a8c1e209"
down_revision: str | None = "f2a9c4e7b106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "painel"
FUSO = "America/Sao_Paulo"
USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
JOB_NAME = "bancaemdia_painel_refresh"

MATERIALIZED_VIEWS = (
    "mv_painel_resumo",
    "mv_painel_por_casa",
    "mv_painel_por_tipster",
    "mv_painel_por_mercado",
    "mv_painel_por_periodo",
    "mv_painel_evolucao_banca",
)

PUBLIC_VIEWS = (
    ("painel_resumo", "mv_painel_resumo"),
    ("painel_por_casa", "mv_painel_por_casa"),
    ("painel_por_tipster", "mv_painel_por_tipster"),
    ("painel_por_mercado", "mv_painel_por_mercado"),
    ("painel_por_periodo", "mv_painel_por_periodo"),
    ("painel_evolucao_banca", "mv_painel_evolucao_banca"),
)


def _metricas(alias: str) -> str:
    """Aggregate the canonical per-bet contributions without using floating point."""
    return f"""
    COALESCE(SUM({alias}.total_apostas), 0)::bigint AS total_apostas,
    COALESCE(SUM({alias}.pendentes), 0)::bigint AS pendentes,
    COALESCE(SUM({alias}.greens), 0)::bigint AS greens,
    COALESCE(SUM({alias}.reds), 0)::bigint AS reds,
    COALESCE(SUM({alias}.giro_centavos), 0)::numeric AS giro_centavos,
    COALESCE(SUM({alias}.base_roi_centavos), 0)::numeric AS base_roi_centavos,
    COALESCE(SUM({alias}.retorno_centavos), 0)::numeric AS retorno_centavos,
    COALESCE(SUM({alias}.lucro_centavos), 0)::numeric AS lucro_centavos,
    COALESCE(SUM({alias}.freebets), 0)::bigint AS freebets,
    CASE
        WHEN COALESCE(SUM({alias}.base_roi_centavos), 0) = 0 THEN 0::numeric
        ELSE SUM({alias}.lucro_centavos) / SUM({alias}.base_roi_centavos)
    END AS roi,
    CASE
        WHEN COALESCE(SUM({alias}.greens + {alias}.reds), 0) = 0 THEN 0::numeric
        ELSE SUM({alias}.greens)::numeric / SUM({alias}.greens + {alias}.reds)
    END AS win_rate
    """.strip()


APOSTAS_METRICAS = f"""
CREATE VIEW {SCHEMA}.apostas_metricas AS
SELECT
    a.usuario_id,
    COALESCE(a.banca_id, 0)::bigint AS banca_id,
    COALESCE(cc.casa_id, 0)::bigint AS casa_id,
    COALESCE(a.tipster_id, 0)::bigint AS tipster_id,
    COALESCE(a.mercado_id, 0)::bigint AS mercado_id,
    ((COALESCE(a.data_aposta, a.criada_em) AT TIME ZONE '{FUSO}')::date) AS data,
    a.estado,
    a.freebet,
    1::bigint AS total_apostas,
    CASE WHEN a.estado = 'PENDENTE' THEN 1 ELSE 0 END::bigint AS pendentes,
    CASE WHEN a.estado = 'GREEN' THEN 1 ELSE 0 END::bigint AS greens,
    CASE WHEN a.estado = 'RED' THEN 1 ELSE 0 END::bigint AS reds,
    CASE
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA') THEN a.stake_centavos::numeric
        ELSE 0::numeric
    END AS giro_centavos,
    CASE
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA') AND a.freebet
            THEN a.valor_aposta_centavos::numeric
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA')
            THEN a.stake_centavos::numeric
        ELSE 0::numeric
    END AS base_roi_centavos,
    CASE
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA')
            THEN COALESCE(a.retorno_centavos, 0)::numeric
        ELSE 0::numeric
    END AS retorno_centavos,
    CASE
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA')
            THEN COALESCE(
                a.retorno_centavos::numeric - a.stake_centavos::numeric,
                0::numeric
            )
        ELSE 0::numeric
    END AS lucro_centavos,
    CASE
        WHEN a.estado NOT IN ('PENDENTE', 'ANULADA') AND a.freebet THEN 1
        ELSE 0
    END::bigint AS freebets
FROM public.apostas AS a
LEFT JOIN public.contas_casa AS cc
    ON cc.id = a.conta_casa_id
   AND cc.usuario_id = a.usuario_id
WHERE a.selecionada IS TRUE
  AND a.revisao_grave IS FALSE
"""


MV_RESUMO = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_resumo AS
WITH metricas AS (
    SELECT
        m.usuario_id,
        {_metricas("m")}
    FROM {SCHEMA}.apostas_metricas AS m
    GROUP BY m.usuario_id
),
primeiro_movimento AS (
    SELECT
        cc.usuario_id,
        cc.id AS conta_casa_id,
        COUNT(m.id)::bigint AS movimentos,
        MIN((m.ocorrido_em AT TIME ZONE '{FUSO}')::date) AS desde,
        COALESCE(SUM(m.valor_centavos), 0)::numeric AS movimento_liquido_centavos
    FROM public.contas_casa AS cc
    LEFT JOIN public.movimentos AS m
        ON m.conta_casa_id = cc.id
       AND m.usuario_id = cc.usuario_id
    GROUP BY cc.usuario_id, cc.id
),
apostas_desde_caixa AS (
    SELECT
        pm.usuario_id,
        pm.conta_casa_id,
        pm.movimentos,
        pm.movimento_liquido_centavos,
        COALESCE(SUM(a.stake_centavos) FILTER (
            WHERE pm.movimentos > 0
              AND ((COALESCE(a.data_aposta, a.criada_em) AT TIME ZONE '{FUSO}')::date) >= pm.desde
        ), 0)::numeric AS apostado_centavos,
        COALESCE(SUM(COALESCE(a.retorno_centavos, 0)) FILTER (
            WHERE pm.movimentos > 0
              AND ((COALESCE(a.data_aposta, a.criada_em) AT TIME ZONE '{FUSO}')::date) >= pm.desde
        ), 0)::numeric AS retornado_centavos
    FROM primeiro_movimento AS pm
    LEFT JOIN public.apostas AS a
        ON a.conta_casa_id = pm.conta_casa_id
       AND a.usuario_id = pm.usuario_id
       AND a.selecionada IS TRUE
    GROUP BY
        pm.usuario_id,
        pm.conta_casa_id,
        pm.movimentos,
        pm.movimento_liquido_centavos
),
contas AS (
    SELECT
        adc.usuario_id,
        adc.conta_casa_id,
        adc.movimentos,
        adc.movimento_liquido_centavos
            - adc.apostado_centavos
            + adc.retornado_centavos AS saldo_bruto_centavos
    FROM apostas_desde_caixa AS adc
),
saldos AS (
    SELECT
        c.usuario_id,
        COUNT(*) FILTER (
            WHERE c.movimentos > 0 AND c.saldo_bruto_centavos >= 0
        )::bigint AS contas_saldo_conhecido,
        COUNT(*) FILTER (
            WHERE c.movimentos = 0 OR c.saldo_bruto_centavos < 0
        )::bigint AS contas_saldo_desconhecido,
        COALESCE(SUM(c.saldo_bruto_centavos) FILTER (
            WHERE c.movimentos > 0 AND c.saldo_bruto_centavos >= 0
        ), 0)::numeric AS saldo_conhecido_centavos,
        CASE
            WHEN COUNT(*) > 0
             AND BOOL_AND(c.movimentos > 0 AND c.saldo_bruto_centavos >= 0)
                THEN SUM(c.saldo_bruto_centavos)
            ELSE NULL::numeric
        END AS saldo_centavos
    FROM contas AS c
    GROUP BY c.usuario_id
)
SELECT
    u.id AS usuario_id,
    COALESCE(m.total_apostas, 0)::bigint AS total_apostas,
    COALESCE(m.pendentes, 0)::bigint AS pendentes,
    COALESCE(m.greens, 0)::bigint AS greens,
    COALESCE(m.reds, 0)::bigint AS reds,
    COALESCE(m.giro_centavos, 0)::numeric AS giro_centavos,
    COALESCE(m.base_roi_centavos, 0)::numeric AS base_roi_centavos,
    COALESCE(m.retorno_centavos, 0)::numeric AS retorno_centavos,
    COALESCE(m.lucro_centavos, 0)::numeric AS lucro_centavos,
    COALESCE(m.freebets, 0)::bigint AS freebets,
    COALESCE(m.roi, 0)::numeric AS roi,
    COALESCE(m.win_rate, 0)::numeric AS win_rate,
    CASE
        WHEN s.usuario_id IS NULL THEN 0::numeric
        ELSE s.saldo_centavos
    END AS saldo_centavos,
    COALESCE(s.saldo_conhecido_centavos, 0)::numeric AS saldo_conhecido_centavos,
    CASE
        WHEN s.usuario_id IS NULL THEN TRUE
        ELSE s.saldo_centavos IS NOT NULL
    END AS saldo_conhecido,
    COALESCE(s.contas_saldo_conhecido, 0)::bigint AS contas_saldo_conhecido,
    COALESCE(s.contas_saldo_desconhecido, 0)::bigint AS contas_saldo_desconhecido
FROM public.usuarios AS u
LEFT JOIN metricas AS m ON m.usuario_id = u.id
LEFT JOIN saldos AS s ON s.usuario_id = u.id
"""


MV_POR_CASA = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_por_casa AS
SELECT
    m.usuario_id,
    m.casa_id,
    COALESCE(c.nome, 'Sem casa')::text AS casa_nome,
    {_metricas("m")}
FROM {SCHEMA}.apostas_metricas AS m
LEFT JOIN public.casas AS c ON c.id = m.casa_id
GROUP BY m.usuario_id, m.casa_id, c.nome
"""


MV_POR_TIPSTER = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_por_tipster AS
SELECT
    m.usuario_id,
    m.tipster_id,
    COALESCE(t.nome, 'Sem tipster')::text AS tipster_nome,
    {_metricas("m")}
FROM {SCHEMA}.apostas_metricas AS m
LEFT JOIN public.tipsters AS t ON t.id = m.tipster_id
GROUP BY m.usuario_id, m.tipster_id, t.nome
"""


MV_POR_MERCADO = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_por_mercado AS
SELECT
    m.usuario_id,
    m.mercado_id,
    COALESCE(mercado.nome, 'Sem mercado')::text AS mercado_nome,
    COALESCE(mercado.familia::text, 'SEM_MERCADO') AS familia,
    {_metricas("m")}
FROM {SCHEMA}.apostas_metricas AS m
LEFT JOIN public.mercados AS mercado ON mercado.id = m.mercado_id
GROUP BY m.usuario_id, m.mercado_id, mercado.nome, mercado.familia
"""


MV_POR_PERIODO = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_por_periodo AS
SELECT
    m.usuario_id,
    periodo.granularidade,
    periodo.periodo_inicio,
    m.casa_id,
    m.tipster_id,
    m.mercado_id,
    {_metricas("m")}
FROM {SCHEMA}.apostas_metricas AS m
CROSS JOIN LATERAL (
    VALUES
        ('dia'::text, m.data),
        ('semana'::text, DATE_TRUNC('week', m.data::timestamp)::date),
        ('mes'::text, DATE_TRUNC('month', m.data::timestamp)::date)
) AS periodo(granularidade, periodo_inicio)
GROUP BY
    m.usuario_id,
    periodo.granularidade,
    periodo.periodo_inicio,
    m.casa_id,
    m.tipster_id,
    m.mercado_id
"""


MV_EVOLUCAO = f"""
CREATE MATERIALIZED VIEW {SCHEMA}.mv_painel_evolucao_banca AS
WITH contribuicoes AS (
    SELECT
        m.usuario_id,
        m.banca_id,
        m.data,
        m.casa_id,
        m.tipster_id,
        m.mercado_id,
        NULL::numeric AS saldo_inicial_centavos,
        m.total_apostas,
        m.pendentes,
        m.greens,
        m.reds,
        m.giro_centavos,
        m.base_roi_centavos,
        m.retorno_centavos,
        m.lucro_centavos,
        m.freebets
    FROM {SCHEMA}.apostas_metricas AS m

    UNION ALL

    SELECT
        b.usuario_id,
        b.id AS banca_id,
        ((b.criado_em AT TIME ZONE '{FUSO}')::date) AS data,
        0::bigint AS casa_id,
        0::bigint AS tipster_id,
        0::bigint AS mercado_id,
        b.saldo_inicial_centavos::numeric,
        0::bigint AS total_apostas,
        0::bigint AS pendentes,
        0::bigint AS greens,
        0::bigint AS reds,
        0::numeric AS giro_centavos,
        0::numeric AS base_roi_centavos,
        0::numeric AS retorno_centavos,
        0::numeric AS lucro_centavos,
        0::bigint AS freebets
    FROM public.bancas AS b
)
SELECT
    c.usuario_id,
    c.banca_id,
    CASE
        WHEN c.banca_id = 0 THEN 'Sem banca'
        ELSE COALESCE(b.nome, 'Banca indisponivel')
    END::text AS banca_nome,
    c.data,
    c.casa_id,
    c.tipster_id,
    c.mercado_id,
    MAX(c.saldo_inicial_centavos) AS saldo_inicial_centavos,
    {_metricas("c")}
FROM contribuicoes AS c
LEFT JOIN public.bancas AS b
    ON b.id = c.banca_id
   AND b.usuario_id = c.usuario_id
GROUP BY
    c.usuario_id,
    c.banca_id,
    b.nome,
    c.data,
    c.casa_id,
    c.tipster_id,
    c.mercado_id
"""


UNIQUE_INDEXES = (
    f"CREATE UNIQUE INDEX uq_mv_painel_resumo ON {SCHEMA}.mv_painel_resumo (usuario_id)",
    "CREATE UNIQUE INDEX uq_mv_painel_por_casa "
    f"ON {SCHEMA}.mv_painel_por_casa (usuario_id, casa_id)",
    "CREATE UNIQUE INDEX uq_mv_painel_por_tipster "
    f"ON {SCHEMA}.mv_painel_por_tipster (usuario_id, tipster_id)",
    "CREATE UNIQUE INDEX uq_mv_painel_por_mercado "
    f"ON {SCHEMA}.mv_painel_por_mercado (usuario_id, mercado_id)",
    "CREATE UNIQUE INDEX uq_mv_painel_por_periodo "
    f"ON {SCHEMA}.mv_painel_por_periodo "
    "(usuario_id, granularidade, periodo_inicio, casa_id, tipster_id, mercado_id)",
    "CREATE UNIQUE INDEX uq_mv_painel_evolucao_banca "
    f"ON {SCHEMA}.mv_painel_evolucao_banca "
    "(usuario_id, banca_id, data, casa_id, tipster_id, mercado_id)",
)

REFRESH_COMMAND = "\n".join((
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;",
    "SET LOCAL statement_timeout = 0;",
    "SELECT pg_advisory_xact_lock(20260930);",
    *(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {SCHEMA}.{nome};" for nome in MATERIALIZED_VIEWS),
    f"UPDATE {SCHEMA}.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1;",
))


def _schedule_sql() -> str:
    return f"""
DO $migration$
DECLARE
    cron_schema text;
    job_exists boolean := false;
    scheduled_job_id bigint;
    refresh_command text := $refresh$
{REFRESH_COMMAND}
$refresh$;
BEGIN
    SELECT n.nspname
      INTO cron_schema
      FROM pg_extension AS e
      JOIN pg_namespace AS n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_cron';

    IF cron_schema IS NULL THEN
        RAISE NOTICE 'pg_cron is not installed; painel refresh must be scheduled externally';
        RETURN;
    END IF;

    BEGIN
        EXECUTE format(
            'SELECT EXISTS ('
            'SELECT 1 FROM %I.job WHERE jobname = $1 AND database = current_database())',
            cron_schema
        ) INTO job_exists USING '{JOB_NAME}';

        IF NOT job_exists THEN
            EXECUTE format('SELECT %I.schedule($1, $2, $3)', cron_schema)
               INTO scheduled_job_id
              USING '{JOB_NAME}', '*/5 * * * *', refresh_command;
            RAISE NOTICE 'scheduled painel refresh as pg_cron job %', scheduled_job_id;
        END IF;
    EXCEPTION
        WHEN insufficient_privilege OR undefined_table OR undefined_function THEN
            RAISE NOTICE 'pg_cron is installed but unavailable to this migration role';
    END;
END
$migration$
"""


def _unschedule_sql() -> str:
    return f"""
DO $migration$
DECLARE
    cron_schema text;
    scheduled_job_id bigint;
BEGIN
    SELECT n.nspname
      INTO cron_schema
      FROM pg_extension AS e
      JOIN pg_namespace AS n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_cron';

    IF cron_schema IS NULL THEN
        RETURN;
    END IF;

    BEGIN
        FOR scheduled_job_id IN EXECUTE format(
            'SELECT jobid FROM %I.job WHERE jobname = $1 AND database = current_database()',
            cron_schema
        ) USING '{JOB_NAME}'
        LOOP
            EXECUTE format('SELECT %I.unschedule($1)', cron_schema) USING scheduled_job_id;
        END LOOP;
    EXCEPTION
        WHEN insufficient_privilege OR undefined_table OR undefined_function THEN
            RAISE NOTICE 'could not inspect or remove the optional pg_cron painel job';
    END;
END
$migration$
"""


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.estado_refresh (
            id smallint PRIMARY KEY,
            atualizado_em timestamp with time zone,
            CONSTRAINT ck_painel_estado_refresh_unico CHECK (id = 1)
        )
        """
    )
    op.execute(f"INSERT INTO {SCHEMA}.estado_refresh (id, atualizado_em) VALUES (1, NULL)")
    op.execute(APOSTAS_METRICAS)

    for definicao in (
        MV_RESUMO,
        MV_POR_CASA,
        MV_POR_TIPSTER,
        MV_POR_MERCADO,
        MV_POR_PERIODO,
        MV_EVOLUCAO,
    ):
        op.execute(definicao)
    for indice in UNIQUE_INDEXES:
        op.execute(indice)

    op.execute(
        f"""
        COMMENT ON TABLE {SCHEMA}.estado_refresh IS
        'The timestamp changes only after all six dashboard materialized views refresh successfully'
        """
    )
    op.execute(f"UPDATE {SCHEMA}.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1")

    for nome_publico, nome_privado in PUBLIC_VIEWS:
        op.execute(
            f"""
            CREATE VIEW public.{nome_publico} WITH (security_barrier = true) AS
            SELECT fonte.*
              FROM {SCHEMA}.{nome_privado} AS fonte
             WHERE fonte.usuario_id = {USUARIO_ATUAL}
            """
        )
    op.execute(
        f"""
        CREATE VIEW public.painel_atualizacao WITH (security_barrier = true) AS
        SELECT estado.atualizado_em
          FROM {SCHEMA}.estado_refresh AS estado
         WHERE {USUARIO_ATUAL} IS NOT NULL
        """
    )

    # The application role only receives access to public views. This also removes any privileges
    # inherited through an unusually permissive database template.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(_schedule_sql())


def downgrade() -> None:
    op.execute(_unschedule_sql())
    op.execute("DROP VIEW IF EXISTS public.painel_atualizacao")
    for nome_publico, _ in reversed(PUBLIC_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS public.{nome_publico}")
    for nome in reversed(MATERIALIZED_VIEWS):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {SCHEMA}.{nome}")
    op.execute(f"DROP VIEW IF EXISTS {SCHEMA}.apostas_metricas")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.estado_refresh")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
