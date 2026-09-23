"""Vincula contas a bancas por tenant e acelera deduplicacao de prints.

Revision ID: a9d6e3f1c210
Revises: f8b2d4a6c901
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d6e3f1c210"
down_revision: str | Sequence[str] | None = "f8b2d4a6c901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_bancas_usuario_id", "bancas", ["usuario_id", "id"])
    op.add_column("contas_casa", sa.Column("banca_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_contas_casa_banca_usuario",
        "contas_casa",
        "bancas",
        ["usuario_id", "banca_id"],
        ["usuario_id", "id"],
    )
    op.create_index("idx_contas_casa_banca", "contas_casa", ["usuario_id", "banca_id"])
    op.create_index(
        "idx_apostas_print_ordem",
        "apostas",
        ["usuario_id", "midia_hash", "ordem_na_mensagem"],
        postgresql_where=sa.text("origem = 'print' AND midia_hash IS NOT NULL"),
    )
    op.add_column("midia_arquivos", sa.Column("s3_key", sa.String(), nullable=True))
    op.execute("""
        DO $migration$
        DECLARE cron_schema text; old_job bigint;
        BEGIN
            SELECT n.nspname INTO cron_schema
              FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE e.extname = 'pg_cron';
            IF cron_schema IS NULL THEN RETURN; END IF;
            BEGIN
                EXECUTE format(
                    'SELECT jobid FROM %I.job WHERE jobname = $1 AND database = current_database()',
                    cron_schema
                ) INTO old_job USING 'bancaemdia_painel_refresh';
                IF old_job IS NOT NULL THEN
                    EXECUTE format('SELECT %I.alter_job($1, $2)', cron_schema)
                    USING old_job, '15 seconds';
                END IF;
            EXCEPTION WHEN insufficient_privilege OR undefined_table OR undefined_function THEN
                RAISE NOTICE 'configure the 15-second painel refresh job as the pg_cron owner';
            END;
        END
        $migration$
    """)


def downgrade() -> None:
    op.drop_column("midia_arquivos", "s3_key")
    op.drop_index("idx_apostas_print_ordem", table_name="apostas")
    op.drop_index("idx_contas_casa_banca", table_name="contas_casa")
    op.drop_constraint("fk_contas_casa_banca_usuario", "contas_casa", type_="foreignkey")
    op.drop_column("contas_casa", "banca_id")
    op.drop_constraint("uq_bancas_usuario_id", "bancas", type_="unique")
