"""005_aposta_selecionada

Revision ID: 9c2d5e7f1a08
Revises: 8f1c4a2b9d33
Create Date: 2026-09-20 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c2d5e7f1a08"
down_revision: str | None = "8f1c4a2b9d33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A coluna existia no projeto antigo e é o que faz "apagar" sobreviver a uma releitura: sem
    # ela, a próxima leitura da mesma foto traria de volta a aposta que a pessoa apagou.
    op.add_column(
        "apostas",
        sa.Column("selecionada", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    # Índice parcial: quase toda aposta está selecionada, e quem se procura é a minoria apagada.
    op.create_index(
        "idx_apostas_apagadas",
        "apostas",
        ["usuario_id", "criada_em"],
        unique=False,
        postgresql_where=sa.text("NOT selecionada"),
    )


def downgrade() -> None:
    op.drop_index("idx_apostas_apagadas", table_name="apostas")
    op.drop_column("apostas", "selecionada")
