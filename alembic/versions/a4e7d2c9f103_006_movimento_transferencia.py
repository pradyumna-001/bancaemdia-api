"""006_movimento_transferencia

Revision ID: a4e7d2c9f103
Revises: 9c2d5e7f1a08
Create Date: 2026-09-21 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e7d2c9f103"
down_revision: str | None = "9c2d5e7f1a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "movimentos",
        sa.Column("transferencia_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Só transferências têm o identificador; não vale carregar depósitos e saques no índice.
    op.create_index(
        "idx_movimentos_transferencia",
        "movimentos",
        ["usuario_id", "transferencia_id"],
        unique=False,
        postgresql_where=sa.text("transferencia_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_movimentos_transferencia", table_name="movimentos")
    op.drop_column("movimentos", "transferencia_id")
