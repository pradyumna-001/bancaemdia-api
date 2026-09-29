"""Join analytics with privacy controls and audit tenant goals.

Revision ID: f158privacy2026
Revises: b6c8e1a4d205, f145audit2026
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f158privacy2026"
down_revision: str | Sequence[str] | None = ("b6c8e1a4d205", "f145audit2026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER audit_metas_desempenho_write "
        "AFTER INSERT OR UPDATE OR DELETE ON metas_desempenho "
        "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
    )
    op.execute(
        "CREATE TRIGGER active_metas_desempenho_write "
        "BEFORE INSERT OR UPDATE ON metas_desempenho "
        "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER active_metas_desempenho_write ON metas_desempenho")
    op.execute("DROP TRIGGER audit_metas_desempenho_write ON metas_desempenho")
