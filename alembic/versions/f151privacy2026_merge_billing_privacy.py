"""Join billing trial/catalog with the reviewed profile and audit history.

Revision ID: f151privacy2026
Revises: d90card2026, f145audit2026
"""

from collections.abc import Sequence

revision: str = "f151privacy2026"
down_revision: str | Sequence[str] | None = ("d90card2026", "f145audit2026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
