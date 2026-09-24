"""Restrict user profiles to the current tenant without breaking signup.

Revision ID: b7d2c9e10101
Revises: a9d6e3f1c210
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7d2c9e10101"
down_revision: str | Sequence[str] | None = "a9d6e3f1c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"


def upgrade() -> None:
    # INSERT ... RETURNING needs a SELECT policy too. The narrowly scoped function
    # creates a profile and returns only that new row; normal SELECT is tenant-only.
    op.execute("DROP POLICY usuarios_leitura ON usuarios")
    op.execute(f"CREATE POLICY usuarios_leitura ON usuarios FOR SELECT USING (id = {TENANT})")
    op.execute("""
        CREATE FUNCTION public.create_user_profile(p_email text, p_nome text)
        RETURNS public.usuarios
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
          INSERT INTO public.usuarios (email, nome)
          VALUES (p_email, p_nome)
          RETURNING *
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.create_user_profile(text,text) FROM PUBLIC")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bancaemdia_app') THEN
            GRANT EXECUTE ON FUNCTION public.create_user_profile(text,text) TO bancaemdia_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION public.create_user_profile(text,text)")
    op.execute("DROP POLICY usuarios_leitura ON usuarios")
    op.execute("CREATE POLICY usuarios_leitura ON usuarios FOR SELECT USING (true)")
