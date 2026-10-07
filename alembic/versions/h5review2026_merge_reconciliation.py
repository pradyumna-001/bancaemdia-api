"""Converge the reviewed product, canonical financial facts and historical journal."""

from pathlib import Path
from runpy import run_path

from alembic import op

revision = "h5review2026"
down_revision = ("h4review2026", "c110fact2026", "r111journal2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Parallel published branches may refresh the panel in either order. The final
    # schema must always count the canonical fact and retain the corrected balance.
    definitions = run_path(str(Path(__file__).with_name("c110fact2026_aposta_consolidacoes.py")))
    definitions["_dashboard"](True)
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    # No fact, source or immutable journal is removed by this merge revision.
    pass
