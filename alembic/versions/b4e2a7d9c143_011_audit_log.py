"""Append-only audit trail for tenant-owned writes.

Revision ID: b4e2a7d9c143
Revises: a9d6e3f1c210
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b4e2a7d9c143"
down_revision: str | Sequence[str] | None = "a9d6e3f1c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "usuarios",
    "bancas",
    "contas_casa",
    "unidades",
    "movimentos",
    "movimento_requisicoes",
    "apostas",
    "eventos",
    "revisao_pendente",
    "coletas_casa",
    "coleta_token",
    "chamadas_ia",
    "uploads",
    "upload_bilhetes",
    "upload_arquivos",
)


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_usuario_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("diff", JSONB(), nullable=False),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("idx_audit_log_usuario_em", "audit_log", ["usuario_id", "timestamp"])
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY audit_log_por_usuario ON audit_log FOR SELECT "
        "USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)"
    )
    op.execute("""
        CREATE FUNCTION audit_log_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $audit$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END
        $audit$
    """)
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()"
    )
    op.execute("""
        CREATE FUNCTION audit_tenant_write() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $audit$
        DECLARE
            old_row jsonb;
            new_row jsonb;
            subject_id bigint;
            actor_id bigint;
            changed jsonb;
            row_id text;
        BEGIN
            IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;
            IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;
            IF TG_TABLE_NAME = 'usuarios' THEN
                subject_id := COALESCE((new_row->>'id')::bigint, (old_row->>'id')::bigint);
            ELSE
                subject_id := COALESCE((new_row->>'usuario_id')::bigint,
                                       (old_row->>'usuario_id')::bigint);
            END IF;
            IF subject_id IS NULL THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            actor_id := NULLIF(current_setting('app.current_user_id', true), '')::bigint;
            row_id := COALESCE(new_row->>'id', old_row->>'id');
            SELECT COALESCE(jsonb_agg(field ORDER BY field), '[]'::jsonb)
              INTO changed
              FROM jsonb_object_keys(COALESCE(new_row, old_row)) AS keys(field)
             WHERE old_row->field IS DISTINCT FROM new_row->field;
            INSERT INTO public.audit_log
                (usuario_id, actor_usuario_id, action, resource_type, resource_id, diff)
            VALUES
                (subject_id, actor_id, TG_OP, TG_TABLE_NAME, row_id,
                 jsonb_build_object('fields', changed));
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $audit$
    """)
    op.execute("""
        CREATE FUNCTION require_active_tenant() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $tenant$
        DECLARE subject_id bigint;
        BEGIN
            subject_id := (to_jsonb(NEW)->>'usuario_id')::bigint;
            -- SHARE locks on the same user coexist across writers. They block a concurrent
            -- deactivation until those writes commit, preserving the active-user invariant.
            IF subject_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.usuarios
                 WHERE id = subject_id AND ativo FOR SHARE
            ) THEN
                RAISE EXCEPTION 'inactive or missing account';
            END IF;
            RETURN NEW;
        END
        $tenant$
    """)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER audit_{table}_write AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
        )
        if table != "usuarios":
            op.execute(
                f"CREATE TRIGGER active_{table}_write BEFORE INSERT OR UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
            )


def downgrade() -> None:
    for table in reversed(TABLES):
        if table != "usuarios":
            op.execute(f"DROP TRIGGER active_{table}_write ON {table}")
        op.execute(f"DROP TRIGGER audit_{table}_write ON {table}")
    op.execute("DROP FUNCTION require_active_tenant()")
    op.execute("DROP FUNCTION audit_tenant_write()")
    op.execute("DROP TRIGGER audit_log_no_change ON audit_log")
    op.execute("DROP FUNCTION audit_log_reject_change()")
    op.execute("DROP POLICY audit_log_por_usuario ON audit_log")
    op.drop_index("idx_audit_log_usuario_em", table_name="audit_log")
    op.drop_table("audit_log")
