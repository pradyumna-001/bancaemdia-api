"""Join accepted main migrations with the published review chain.

Published revision IDs and their dependencies remain unchanged.
"""

revision = "j4main2026"
down_revision = ("h4review2026", "j168base2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
