"""Join durable events with checkout/privacy migration history.

Revision ID: f153events2026
Revises: d92events2026, f152checkout2026
"""

from collections.abc import Sequence

revision: str = "f153events2026"
down_revision: str | Sequence[str] | None = ("d92events2026", "f152checkout2026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
