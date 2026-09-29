"""Do not report balances as known when bets predate the first cash movement.

Revision ID: b71c6a93e402
Revises: b7d2c9e10101
"""

from collections.abc import Sequence
from pathlib import Path
from runpy import run_path

from alembic import op

revision: str = "b71c6a93e402"
down_revision: str | Sequence[str] | None = "b7d2c9e10101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "painel"
USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("the original painel summary definition changed")
    return source.replace(old, new)


def _corrected_summary() -> str:
    # Revision 008 is immutable: derive the replacement from its exact SQL rather than editing
    # a migration that may already have been applied to a development database.
    source = run_path(
        str(Path(__file__).with_name("d3f6a8c1e209_008_painel_materialized_views.py"))
    )["MV_RESUMO"]
    if not isinstance(source, str):
        raise RuntimeError("the original painel summary SQL is unavailable")
    source = _replace_once(
        source,
        "        ), 0)::numeric AS retornado_centavos\n    FROM primeiro_movimento AS pm",
        """        ), 0)::numeric AS retornado_centavos,
        COUNT(a.id) FILTER (
            WHERE pm.movimentos > 0
              AND ((COALESCE(a.data_aposta, a.criada_em) AT TIME ZONE 'America/Sao_Paulo')::date)
                  < pm.desde
        )::bigint AS apostas_antes_do_caixa
    FROM primeiro_movimento AS pm""",
    )
    source = _replace_once(
        source,
        "        adc.movimentos,\n        adc.movimento_liquido_centavos",
        "        adc.movimentos,\n        adc.apostas_antes_do_caixa,\n"
        "        adc.movimento_liquido_centavos",
    )
    known = "c.movimentos > 0 AND c.saldo_bruto_centavos >= 0"
    if source.count(known) != 3:
        raise RuntimeError("the original known-balance predicates changed")
    source = source.replace(
        known,
        "c.movimentos > 0 AND c.apostas_antes_do_caixa = 0 AND c.saldo_bruto_centavos >= 0",
    )
    source = _replace_once(
        source,
        "c.movimentos = 0 OR c.saldo_bruto_centavos < 0",
        "c.movimentos = 0 OR c.apostas_antes_do_caixa > 0 OR c.saldo_bruto_centavos < 0",
    )
    return source


def _public_view(source: str) -> str:
    return f"""
        CREATE OR REPLACE VIEW public.painel_resumo WITH (security_barrier = true) AS
        SELECT fonte.*
          FROM {SCHEMA}.{source} AS fonte
         WHERE fonte.usuario_id = {USUARIO_ATUAL}
    """


def upgrade() -> None:
    # The refresh CLI and optional pg_cron job use this same advisory key.
    op.execute("SELECT pg_advisory_xact_lock(20260930)")
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute(
        f"ALTER MATERIALIZED VIEW {SCHEMA}.mv_painel_resumo RENAME TO mv_painel_resumo_legacy"
    )
    op.execute(_corrected_summary())
    op.execute(
        f"CREATE UNIQUE INDEX uq_mv_painel_resumo_v2 ON {SCHEMA}.mv_painel_resumo (usuario_id)"
    )
    op.execute(f"REVOKE ALL ON {SCHEMA}.mv_painel_resumo FROM PUBLIC")
    op.execute(_public_view("mv_painel_resumo"))


def downgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(20260930)")
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute(_public_view("mv_painel_resumo_legacy"))
    op.execute(f"DROP MATERIALIZED VIEW {SCHEMA}.mv_painel_resumo")
    op.execute(
        f"ALTER MATERIALIZED VIEW {SCHEMA}.mv_painel_resumo_legacy RENAME TO mv_painel_resumo"
    )
    op.execute(f"REFRESH MATERIALIZED VIEW {SCHEMA}.mv_painel_resumo")
