"""Append-only tenant-isolated historical reconciliation checkpoints.

Revision ID: r111journal2026
Revises: a9d6e3f1c210
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "r111journal2026"
down_revision = "a9d6e3f1c210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliacao_chunks",
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), primary_key=True),
        sa.Column("report_sha256", sa.String(64), primary_key=True),
        sa.Column("chunk_index", sa.Integer(), primary_key=True),
        sa.Column("next_offset", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("audit", JSONB(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("chunk_index >= 0 AND next_offset >= 0", name="ck_r111_offset"),
        sa.CheckConstraint("batch_size BETWEEN 1 AND 200", name="ck_r111_batch"),
        sa.CheckConstraint("report_sha256 ~ '^[0-9a-f]{64}$'", name="ck_r111_hash"),
    )
    op.execute("ALTER TABLE reconciliacao_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliacao_chunks FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY reconciliacao_chunks_tenant ON reconciliacao_chunks
        USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
        WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
    """)
    op.execute("""
        CREATE FUNCTION r111_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'reconciliation journal is append-only'; END $$
    """)
    op.execute("""
        CREATE TRIGGER r111_immutable BEFORE UPDATE OR DELETE ON reconciliacao_chunks
        FOR EACH ROW EXECUTE FUNCTION r111_immutable()
    """)
    # Match an existing deployment's app grants without inventing a privileged operator role.
    op.execute("""
        DO $$ DECLARE role_name text; BEGIN
          FOR role_name IN SELECT DISTINCT grantee FROM information_schema.role_table_grants
            WHERE table_schema='public' AND table_name='apostas' AND privilege_type='SELECT'
              AND grantee <> 'PUBLIC'
          LOOP EXECUTE format('GRANT SELECT, INSERT ON reconciliacao_chunks TO %I', role_name);
          END LOOP;
        END $$
    """)


def downgrade() -> None:
    # Server-side guard also renders in Alembic offline SQL; no fake offline SELECT result.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM reconciliacao_chunks) THEN
            RAISE EXCEPTION 'Preserve reconciliation audit; rollback application, not journal';
          END IF;
        END $$
    """)
    op.drop_table("reconciliacao_chunks")
    op.execute("DROP FUNCTION r111_immutable()")
