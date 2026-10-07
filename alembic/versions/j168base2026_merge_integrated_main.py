"""Join accepted main migrations with the published review chain.

Published revision IDs and their dependencies remain unchanged.
"""

revision = "j168base2026"
down_revision = ("h2review2026", "f107main2026", "f168main2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
