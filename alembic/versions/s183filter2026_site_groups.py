"""Private site filter groups; historical bets remain unassigned.

Revision ID: s183filter2026
Revises: j6main2026
"""

import sqlalchemy as sa
from alembic import op

revision = "s183filter2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grupos_aposta",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("nome", sa.String(160), nullable=False),
        sa.Column("arquivado", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("usuario_id", "id", name="uq_grupos_aposta_usuario_id"),
        sa.UniqueConstraint("usuario_id", "nome", name="uq_grupos_aposta_nome"),
        sa.CheckConstraint("length(btrim(nome)) BETWEEN 1 AND 160", name="ck_grupos_aposta_nome"),
    )
    op.create_index("idx_grupos_aposta_usuario", "grupos_aposta", ["usuario_id", "arquivado"])
    op.create_table(
        "apostas_grupos",
        sa.Column("usuario_id", sa.BigInteger(), primary_key=True),
        sa.Column("aposta_id", sa.BigInteger(), primary_key=True),
        sa.Column("grupo_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "vinculado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id", "grupo_id"],
            ["grupos_aposta.usuario_id", "grupos_aposta.id"],
            name="fk_apostas_grupos_grupo_usuario",
        ),
        sa.ForeignKeyConstraint(
            ["aposta_id", "usuario_id"],
            ["apostas.id", "apostas.usuario_id"],
            name="fk_apostas_grupos_aposta_usuario",
        ),
    )
    op.create_index(
        "idx_apostas_grupos_selecao", "apostas_grupos", ["usuario_id", "grupo_id", "aposta_id"]
    )
    op.execute("ALTER TABLE grupos_aposta ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE grupos_aposta FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE apostas_grupos ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE apostas_grupos FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY grupos_aposta_por_usuario ON grupos_aposta FOR ALL USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint) WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)"
    )
    op.execute(
        "CREATE POLICY apostas_grupos_por_usuario ON apostas_grupos FOR ALL USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint) WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)"
    )
    op.execute(
        "CREATE TRIGGER audit_grupos_aposta_write AFTER INSERT OR UPDATE OR DELETE ON grupos_aposta "
        "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
    )
    for tabela in ("grupos_aposta", "apostas_grupos"):
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {tabela} "
            "FOR EACH ROW EXECUTE FUNCTION public.billing_require_write()"
        )
        op.execute(
            f"CREATE TRIGGER active_{tabela}_write BEFORE INSERT OR UPDATE ON {tabela} "
            "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
        )
    # The existing audit table permits SELECT only to the application role. A
    # database trigger records membership atomically, including its composite ID.
    op.execute("""
        CREATE FUNCTION audit_site_group_link() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $audit$
        DECLARE row_data jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN row_data := to_jsonb(OLD);
            ELSE row_data := to_jsonb(NEW); END IF;
            INSERT INTO public.audit_log
                (usuario_id, actor_usuario_id, action, resource_type, resource_id, diff)
            VALUES (
                (row_data->>'usuario_id')::bigint,
                NULLIF(current_setting('app.current_user_id', true), '')::bigint,
                TG_OP, 'apostas_grupos',
                (row_data->>'aposta_id') || ':' || (row_data->>'grupo_id'),
                jsonb_build_object('aposta_id', row_data->>'aposta_id',
                                   'grupo_id', row_data->>'grupo_id')
            );
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $audit$
    """)
    op.execute(
        "CREATE TRIGGER audit_apostas_grupos_write AFTER INSERT OR UPDATE OR DELETE ON apostas_grupos "
        "FOR EACH ROW EXECUTE FUNCTION audit_site_group_link()"
    )


def downgrade() -> None:
    op.drop_table("apostas_grupos")
    op.execute("DROP FUNCTION audit_site_group_link()")
    op.drop_table("grupos_aposta")
