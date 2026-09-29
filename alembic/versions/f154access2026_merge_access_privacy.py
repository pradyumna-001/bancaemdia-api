"""Join billing access guards with privacy and checkout migrations."""

from alembic import op

revision = "f154access2026"
down_revision = ("d93access2026", "f153events2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SELECT billing_install_write_guards()")
    # Analytics may be installed first. Its reciprocal hook handles the reverse order.
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('public.metas_desempenho') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass('public.metas_desempenho')
                    AND tgname='billing_write_guard' AND NOT tgisinternal
            ) THEN
                CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE
                    ON public.metas_desempenho FOR EACH ROW
                    EXECUTE FUNCTION public.billing_require_write();
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('public.metas_desempenho') IS NOT NULL THEN
                DROP TRIGGER IF EXISTS billing_write_guard ON public.metas_desempenho;
            END IF;
        END $$
    """)
