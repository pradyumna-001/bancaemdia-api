"""Join the reviewed audit and private-profile/dashboard branches.

Revision ID: f145audit2026
Revises: b4e2a7d9c143, b71c6a93e402

Both histories remain immutable. Their upgrades operate on distinct objects;
this revision gives deployments one head after both prerequisites are applied.
"""

from collections.abc import Sequence

revision: str = "f145audit2026"
down_revision: str | Sequence[str] | None = ("b4e2a7d9c143", "b71c6a93e402")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
