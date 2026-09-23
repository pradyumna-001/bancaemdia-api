from __future__ import annotations

import io
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "f2a9c4e7b106"
PAINEL = "d3f6a8c1e209"
HEAD = "c90b1a7e2026"
MATERIALIZED_VIEWS = (
    "mv_painel_resumo",
    "mv_painel_por_casa",
    "mv_painel_por_tipster",
    "mv_painel_por_mercado",
    "mv_painel_por_periodo",
    "mv_painel_evolucao_banca",
)
PUBLIC_VIEWS = (
    "painel_resumo",
    "painel_por_casa",
    "painel_por_tipster",
    "painel_por_mercado",
    "painel_por_periodo",
    "painel_evolucao_banca",
)
UNIQUE_KEYS = {
    "uq_mv_painel_resumo": "usuario_id",
    "uq_mv_painel_por_casa": "usuario_id, casa_id",
    "uq_mv_painel_por_tipster": "usuario_id, tipster_id",
    "uq_mv_painel_por_mercado": "usuario_id, mercado_id",
    "uq_mv_painel_por_periodo": (
        "usuario_id, granularidade, periodo_inicio, casa_id, tipster_id, mercado_id"
    ),
    "uq_mv_painel_evolucao_banca": ("usuario_id, banca_id, data, casa_id, tipster_id, mercado_id"),
}


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _upgrade_sql() -> str:
    buffer = io.StringIO()
    command.upgrade(_config(buffer), f"{PREVIOUS}:{PAINEL}", sql=True)
    return buffer.getvalue()


def _downgrade_sql() -> str:
    buffer = io.StringIO()
    command.downgrade(_config(buffer), f"{PAINEL}:{PREVIOUS}", sql=True)
    return buffer.getvalue()


def test_painel_revision_keeps_its_published_identity_and_parent() -> None:
    script = ScriptDirectory.from_config(_config())
    painel = script.get_revision(PAINEL)

    assert script.get_current_head() == HEAD
    assert painel.down_revision == PREVIOUS
    assert "008_painel_materialized_views" in painel.doc


def test_private_schema_has_the_six_requested_materialized_views() -> None:
    sql = _upgrade_sql()

    assert "CREATE SCHEMA painel" in sql
    assert "REVOKE ALL ON SCHEMA painel FROM PUBLIC" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA painel FROM PUBLIC" in sql
    assert sql.count("CREATE MATERIALIZED VIEW painel.") == len(MATERIALIZED_VIEWS)
    for name in MATERIALIZED_VIEWS:
        assert f"CREATE MATERIALIZED VIEW painel.{name} AS" in sql


def test_every_materialized_view_has_a_total_column_only_unique_index() -> None:
    sql = _upgrade_sql()

    indexes = re.findall(r"CREATE UNIQUE INDEX (uq_mv_painel_\w+) ON painel\.\w+ \(([^)]+)\);", sql)
    assert dict(indexes) == UNIQUE_KEYS
    for index_name, _ in indexes:
        statement = next(
            line for line in sql.splitlines() if f"CREATE UNIQUE INDEX {index_name} " in line
        )
        assert " WHERE " not in statement
        assert "COALESCE" not in statement


def test_public_security_barrier_views_are_the_only_application_surface() -> None:
    sql = _upgrade_sql()
    tenant_filter = (
        "fonte.usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint"
    )

    for name in PUBLIC_VIEWS:
        assert f"CREATE VIEW public.{name} WITH (security_barrier = true) AS" in sql
    assert sql.count(tenant_filter) == len(PUBLIC_VIEWS)
    assert "CREATE VIEW public.painel_atualizacao WITH (security_barrier = true) AS" in sql
    assert "NULLIF(current_setting('app.current_user_id', true), '')::bigint IS NOT NULL" in sql
    assert "GRANT" not in sql


def test_canonical_financial_rules_are_expressed_once_in_the_private_base_view() -> None:
    sql = _upgrade_sql()
    base_start = sql.index("CREATE VIEW painel.apostas_metricas AS")
    base_end = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_resumo AS")
    base = sql[base_start:base_end]
    normalizado = " ".join(base.split())

    assert "WHERE a.selecionada IS TRUE" in base
    assert "AND a.revisao_grave IS FALSE" in base
    assert "a.estado NOT IN ('PENDENTE', 'ANULADA')" in base
    assert "THEN a.valor_aposta_centavos::numeric" in base
    assert (
        "COALESCE( a.retorno_centavos::numeric - a.stake_centavos::numeric, "
        "0::numeric )" in normalizado
    )
    assert "CASE WHEN a.estado = 'GREEN' THEN 1 ELSE 0 END" in base
    assert "CASE WHEN a.estado = 'RED' THEN 1 ELSE 0 END" in base
    assert "AT TIME ZONE 'America/Sao_Paulo'" in base
    assert "::float" not in base.lower()


def test_period_cube_has_daily_weekly_and_monthly_rows_with_dimension_sentinels() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_por_periodo AS")
    end = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_evolucao_banca AS")
    period = sql[start:end]

    assert "('dia'::text, m.data)" in period
    assert "('semana'::text, DATE_TRUNC('week', m.data::timestamp)::date)" in period
    assert "('mes'::text, DATE_TRUNC('month', m.data::timestamp)::date)" in period
    assert "COALESCE(cc.casa_id, 0)::bigint AS casa_id" in sql
    for dimension in ("tipster_id", "mercado_id"):
        assert f"COALESCE(a.{dimension}, 0)::bigint AS {dimension}" in sql
    for dimension in ("casa_id", "tipster_id", "mercado_id"):
        assert f"m.{dimension}" in period


def test_bankroll_evolution_keeps_bank_and_filter_dimensions_as_daily_contributions() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_evolucao_banca AS")
    end = sql.index("CREATE UNIQUE INDEX uq_mv_painel_resumo")
    evolution = sql[start:end]

    assert "FROM public.bancas AS b" in evolution
    assert "b.saldo_inicial_centavos::numeric" in evolution
    assert "0::bigint AS casa_id" in evolution
    assert "0::bigint AS tipster_id" in evolution
    assert "0::bigint AS mercado_id" in evolution
    assert "MAX(c.saldo_inicial_centavos) AS saldo_inicial_centavos" in evolution
    assert "AS lucro_centavos" in evolution
    assert "AS giro_centavos" in evolution
    assert "acumulado" not in evolution.lower()


def test_summary_balance_is_null_unless_every_account_matches_temporal_balance_rules() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_resumo AS")
    end = sql.index("CREATE MATERIALIZED VIEW painel.mv_painel_por_casa AS")
    summary = sql[start:end]

    assert "COUNT(m.id)::bigint AS movimentos" in summary
    assert "a.selecionada IS TRUE" in summary
    assert "pm.movimento_liquido_centavos" in summary
    assert "- adc.apostado_centavos" in summary
    assert "+ adc.retornado_centavos AS saldo_bruto_centavos" in summary
    assert "BOOL_AND(c.movimentos > 0 AND c.saldo_bruto_centavos >= 0)" in summary
    assert "AS saldo_conhecido_centavos" in summary
    assert "ELSE NULL::numeric" in summary
    assert "WHEN s.usuario_id IS NULL THEN 0::numeric" in summary
    assert "WHEN s.usuario_id IS NULL THEN TRUE" in summary
    assert "AS saldo_conhecido" in summary
    assert "AS contas_saldo_desconhecido" in summary


def test_refresh_job_is_optional_atomic_and_records_only_a_complete_refresh() -> None:
    sql = _upgrade_sql()

    assert "CREATE EXTENSION" not in sql
    assert "WHERE e.extname = 'pg_cron'" in sql
    assert "bancaemdia_painel_refresh" in sql
    assert "*/5 * * * *" in sql
    isolation = sql.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    timeout = sql.index("SET LOCAL statement_timeout = 0")
    lock = sql.index("SELECT pg_advisory_xact_lock(20260930)")
    refreshes = [
        sql.index(f"REFRESH MATERIALIZED VIEW CONCURRENTLY painel.{name}")
        for name in MATERIALIZED_VIEWS
    ]
    final_state = sql.rindex(
        "UPDATE painel.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1;"
    )
    assert isolation < timeout < lock < min(refreshes)
    assert refreshes == sorted(refreshes)
    assert max(refreshes) < final_state


def test_real_refresh_timestamp_is_private_and_exposed_through_an_authenticated_view() -> None:
    sql = _upgrade_sql()

    assert "CREATE TABLE painel.estado_refresh" in sql
    assert "CONSTRAINT ck_painel_estado_refresh_unico CHECK (id = 1)" in sql
    assert "INSERT INTO painel.estado_refresh (id, atualizado_em) VALUES (1, NULL)" in sql
    assert sql.count("SET atualizado_em = clock_timestamp() WHERE id = 1") == 2
    assert "now() - pg_last_xact_replay_timestamp()" not in sql


def test_downgrade_removes_the_optional_job_public_surface_and_private_schema() -> None:
    sql = _downgrade_sql()

    assert "bancaemdia_painel_refresh" in sql
    assert ".unschedule($1)" in sql
    assert "DROP EXTENSION" not in sql
    assert sql.index(".unschedule($1)") < sql.index("DROP VIEW IF EXISTS public.painel_atualizacao")
    for name in PUBLIC_VIEWS:
        assert f"DROP VIEW IF EXISTS public.{name}" in sql
    for name in MATERIALIZED_VIEWS:
        assert f"DROP MATERIALIZED VIEW IF EXISTS painel.{name}" in sql
    assert "DROP VIEW IF EXISTS painel.apostas_metricas" in sql
    assert "DROP TABLE IF EXISTS painel.estado_refresh" in sql
    assert "DROP SCHEMA IF EXISTS painel" in sql
