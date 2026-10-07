"""Join accepted main migrations with the published review chain.

Published revision IDs and their dependencies remain unchanged.
"""

revision = "j6main2026"
down_revision = ("h6review2026", "j3main2026", "j5main2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
