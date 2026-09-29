"""Join the checkout and reviewed billing/privacy histories.

Revision ID: f152checkout2026
Revises: d91checkout2026, f151privacy2026
"""

from collections.abc import Sequence

revision: str = "f152checkout2026"
down_revision: str | Sequence[str] | None = ("d91checkout2026", "f151privacy2026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
