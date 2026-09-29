"""Extend the canonical dashboard source and add tenant goals.

Revision ID: b6c8e1a4d205
Revises: a9d6e3f1c210
"""

from collections.abc import Sequence
from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic import op

revision: str = "b6c8e1a4d205"
down_revision: str | Sequence[str] | None = "a9d6e3f1c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"


def _painel_original() -> dict[str, object]:
    # Reuse the exact financial inclusion rules from #30. The additional columns are
    # attributes only; none of its monetary expressions is defined a second time.
    arquivo = Path(__file__).with_name("d3f6a8c1e209_008_painel_materialized_views.py")
    return run_path(str(arquivo))


def _fonte_original() -> str:
    return str(_painel_original()["APOSTAS_METRICAS"])


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column(
            "fuso_horario",
            sa.String(64),
            nullable=False,
            server_default="America/Sao_Paulo",
        ),
    )
    op.create_table(
        "metas_desempenho",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("titulo", sa.String(120), nullable=False),
        sa.Column("metrica", sa.String(32), nullable=False),
        sa.Column("inicio", sa.Date(), nullable=False),
        sa.Column("fim", sa.Date(), nullable=False),
        sa.Column("alvo", sa.Numeric(24, 6), nullable=False),
        sa.Column("linha_base", sa.Numeric(24, 6), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ativa"),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "atualizada_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("inicio <= fim", name="ck_metas_desempenho_intervalo"),
        sa.CheckConstraint(
            "metrica IN ('lucro_centavos', 'giro_centavos', 'roi', 'win_rate', 'total_apostas')",
            name="ck_metas_desempenho_metrica",
        ),
        sa.CheckConstraint(
            "status IN ('ativa', 'concluida', 'arquivada')",
            name="ck_metas_desempenho_status",
        ),
    )
    op.create_index("ix_metas_desempenho_usuario", "metas_desempenho", ["usuario_id", "status"])
    op.execute("ALTER TABLE metas_desempenho ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE metas_desempenho FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY metas_desempenho_por_usuario ON metas_desempenho FOR ALL "
        f"USING (usuario_id = {USUARIO_ATUAL}) WITH CHECK (usuario_id = {USUARIO_ATUAL})"
    )

    original = _fonte_original()
    estendido = original.replace(
        "CREATE VIEW painel.apostas_metricas AS",
        "CREATE OR REPLACE VIEW painel.apostas_metricas AS",
    )
    marcador = "FROM public.apostas AS a"
    assert estendido.count(marcador) == 1
    estendido = estendido.replace(
        marcador,
        """    , a.id AS aposta_id,
    COALESCE(a.data_aposta, a.criada_em) AS instante,
    a.odd::numeric AS odd,
    (CASE WHEN a.freebet THEN a.valor_aposta_centavos ELSE a.stake_centavos END)::numeric
        AS valor_face_centavos,
    COALESCE(a.competicao_id, 0)::bigint AS competicao_id
FROM public.apostas AS a""",
    )
    op.execute(estendido)
    op.execute(
        f"""
        CREATE VIEW public.painel_analises_apostas WITH (security_barrier = true) AS
        SELECT m.*, e.id AS esporte_id, e.nome AS esporte_nome,
               b.nome AS banca_nome, b.saldo_inicial_centavos AS capital_banca_centavos
        FROM painel.apostas_metricas AS m
        LEFT JOIN public.competicoes AS c ON c.id = NULLIF(m.competicao_id, 0)
        LEFT JOIN public.esportes AS e ON e.id = c.esporte_id
        LEFT JOIN public.bancas AS b ON b.id = NULLIF(m.banca_id, 0)
            AND b.usuario_id = m.usuario_id
        WHERE m.usuario_id = {USUARIO_ATUAL}
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW public.painel_analises_apostas")
    anterior = _painel_original()
    for publico, _ in reversed(anterior["PUBLIC_VIEWS"]):
        op.execute(f"DROP VIEW public.{publico}")
    for nome in reversed(anterior["MATERIALIZED_VIEWS"]):
        op.execute(f"DROP MATERIALIZED VIEW painel.{nome}")
    op.execute("DROP VIEW painel.apostas_metricas")
    op.execute(str(anterior["APOSTAS_METRICAS"]))
    for nome in (
        "MV_RESUMO",
        "MV_POR_CASA",
        "MV_POR_TIPSTER",
        "MV_POR_MERCADO",
        "MV_POR_PERIODO",
        "MV_EVOLUCAO",
    ):
        op.execute(str(anterior[nome]))
    for indice in anterior["UNIQUE_INDEXES"]:
        op.execute(str(indice))
    for publico, privado in anterior["PUBLIC_VIEWS"]:
        op.execute(
            f"CREATE VIEW public.{publico} WITH (security_barrier = true) AS "
            f"SELECT fonte.* FROM painel.{privado} AS fonte WHERE fonte.usuario_id = {USUARIO_ATUAL}"
        )
    op.execute("UPDATE painel.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1")
    op.execute("DROP POLICY metas_desempenho_por_usuario ON metas_desempenho")
    op.drop_table("metas_desempenho")
    op.drop_column("usuarios", "fuso_horario")
