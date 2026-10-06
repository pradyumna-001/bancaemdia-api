"""Converge billing access and holder accounts without rewriting published IDs."""

revision = "h2review2026"
down_revision = ("g93tenant2026", "f134billing2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    pass
