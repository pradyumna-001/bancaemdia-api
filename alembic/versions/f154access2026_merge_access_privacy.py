"""Join billing access guards with privacy and checkout migrations."""

from alembic import op

revision = "f154access2026"
down_revision = ("d93access2026", "f153events2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    pass
