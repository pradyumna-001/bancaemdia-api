"""009_merge_painel_caixa

Revision ID: f8b2d4a6c901
Revises: d3f6a8c1e209, e5a1c7d9b204
Create Date: 2026-09-22 16:15:00

"""

from collections.abc import Sequence

revision: str = "f8b2d4a6c901"
down_revision: str | Sequence[str] | None = ("d3f6a8c1e209", "e5a1c7d9b204")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Une os heads sem reaplicar as alterações de painel ou caixa."""


def downgrade() -> None:
    """Alembic restaura os dois heads anteriores ao remover a revisão de merge."""
