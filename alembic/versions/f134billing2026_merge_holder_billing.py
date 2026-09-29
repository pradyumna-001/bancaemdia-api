"""Join holder history with the current card-confirmed billing foundation.

Revision ID: f134billing2026
Revises: d94a7b1c2026, f151privacy2026
"""

from collections.abc import Sequence

revision: str = "f134billing2026"
down_revision: str | Sequence[str] | None = ("d94a7b1c2026", "f151privacy2026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
