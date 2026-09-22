"""008_revisao_resolvida_evento

Revision ID: f2a9c4e7b106
Revises: c7b1e9a42d60
Create Date: 2026-09-21 16:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2a9c4e7b106"
down_revision: str | None = "c7b1e9a42d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIPOS_ANTES = (
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
)
TIPOS_DEPOIS = (*TIPOS_ANTES, "REVISAO_RESOLVIDA")


def _regra(tipos: tuple[str, ...]) -> str:
    return f"tipo IN ({', '.join(repr(tipo) for tipo in tipos)})"


def _trocar_regra(tipos: tuple[str, ...]) -> None:
    op.drop_constraint("ck_eventos_tipo", "eventos", type_="check")
    op.create_check_constraint("ck_eventos_tipo", "eventos", _regra(tipos))


def upgrade() -> None:
    _trocar_regra(TIPOS_DEPOIS)


def downgrade() -> None:
    # A versão anterior da aplicação ignora eventos desconhecidos ao projetar a aposta. Manter o
    # valor aceito é compatível com ela e evita que um rollback apague auditoria ou falhe depois da
    # primeira revisão resolvida.
    _trocar_regra(TIPOS_DEPOIS)
