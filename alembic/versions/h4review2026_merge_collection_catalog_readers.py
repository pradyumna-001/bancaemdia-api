"""Converge reviewed collection, temporal accounts, catalog and reader quarantine.

Published migration IDs and parent relationships remain unchanged.
"""

revision = "h4review2026"
down_revision = ("f107main2026", "c108v22026", "h2review2026", "c113catalog2026", "c114reader2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
