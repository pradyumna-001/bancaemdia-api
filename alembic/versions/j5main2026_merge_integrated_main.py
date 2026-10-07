"""Join accepted main migrations with the published review chain.

Published revision IDs and their dependencies remain unchanged.
"""

revision = "j5main2026"
down_revision = ("h5review2026", "j4main2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
