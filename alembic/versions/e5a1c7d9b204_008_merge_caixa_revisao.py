"""008_merge_caixa_revisao

Revision ID: e5a1c7d9b204
Revises: c7b1e9a42d60, f2a9c4e7b106
Create Date: 2026-09-22 16:00:00

"""

from collections.abc import Sequence

revision: str = "e5a1c7d9b204"
down_revision: str | Sequence[str] | None = ("c7b1e9a42d60", "f2a9c4e7b106")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Une as duas linhas sem repetir alterações já aplicadas."""


def downgrade() -> None:
    """Alembic restaura os dois heads anteriores ao remover a revisão de merge."""
