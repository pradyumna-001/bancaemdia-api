"""Join the six site migrations merged on top of j6main2026.

Published revision IDs and their dependencies remain unchanged.
"""

revision = "j7main2026"
down_revision = (
    "a212lote2026",
    "b211banca2026",
    "m202manual2026",
    "n115native2026",
    "r200review2026",
    "s183filter2026",
    "u210unit2026",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
