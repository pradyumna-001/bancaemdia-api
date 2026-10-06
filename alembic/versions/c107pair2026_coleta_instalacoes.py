"""Independent collection installations and one-time pairing, with legacy revocation."""

import sqlalchemy as sa
from alembic import op

revision = "c107pair2026"
down_revision = "a9d6e3f1c210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coleta_instalacoes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("instalacao_publica_id", sa.UUID(), nullable=False),
        sa.Column("nome_dispositivo", sa.String(80)),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("token_prefixo", sa.String(12)),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *[
            sa.Column(name, sa.DateTime(timezone=True))
            for name in (
                "pareado_em",
                "ultimo_uso_em",
                "rotacionado_em",
                "revogado_em",
                "expira_em",
            )
        ],
        sa.Column("token_legado_id", sa.BigInteger(), unique=True),
        sa.UniqueConstraint(
            "usuario_id", "instalacao_publica_id", name="uq_coleta_instalacao_owner_public"
        ),
    )
    op.create_table(
        "coleta_pairing_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("code_hash", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumido_em", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "expira_em > criado_em AND expira_em <= criado_em + interval '30 minutes'",
            name="ck_pairing_code_lifetime",
        ),
    )
    op.create_index("ix_coleta_pairing_codes_expira_em", "coleta_pairing_codes", ["expira_em"])
    op.create_table(
        "coleta_pairing_quotas",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_coleta_pairing_quotas_expires_at", "coleta_pairing_quotas", ["expires_at"])
    # A legacy credential has no proven device identity. Inventory it, disable it,
    # and require explicit pairing; never copy one live hash into several devices.
    op.execute("""INSERT INTO coleta_instalacoes
        (usuario_id,instalacao_publica_id,nome_dispositivo,criado_em,revogado_em,token_legado_id)
        SELECT usuario_id,gen_random_uuid(),'Legado: novo pareamento necessário',criado_em,now(),id
        FROM coleta_token""")
    op.execute("UPDATE coleta_token SET ativo=false")
    for table in ("coleta_instalacoes", "coleta_pairing_codes", "coleta_pairing_quotas"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table in ("coleta_instalacoes", "coleta_pairing_codes"):
        op.execute(
            f"CREATE POLICY {table}_owner ON {table} FOR ALL "
            "USING (usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint) "
            "WITH CHECK (usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)"
        )
    op.execute("""CREATE POLICY coleta_instalacoes_lookup ON coleta_instalacoes FOR SELECT
        USING (token_hash=NULLIF(current_setting('app.coleta_token_hash',true),''))""")
    op.execute("""CREATE POLICY coleta_pairing_lookup ON coleta_pairing_codes FOR SELECT
        USING (code_hash=NULLIF(current_setting('app.coleta_pairing_hash',true),''))""")
    op.execute("""CREATE FUNCTION coleta_pairing_limit(bucket text, maximum integer, seconds integer)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE amount integer;
        BEGIN
          IF length(bucket)<>64 OR maximum NOT BETWEEN 1 AND 100000 OR seconds NOT BETWEEN 1 AND 86400
          THEN RAISE EXCEPTION 'invalid quota configuration'; END IF;
          DELETE FROM public.coleta_pairing_quotas WHERE id IN (
            SELECT id FROM public.coleta_pairing_quotas WHERE expires_at<clock_timestamp()
            LIMIT 100 FOR UPDATE SKIP LOCKED);
          INSERT INTO public.coleta_pairing_quotas(key,used,expires_at)
            VALUES(bucket,1,clock_timestamp()+seconds*interval '1 second')
          ON CONFLICT(key) DO UPDATE SET used=CASE WHEN coleta_pairing_quotas.expires_at<=clock_timestamp()
              THEN 1 ELSE coleta_pairing_quotas.used+1 END,
            expires_at=CASE WHEN coleta_pairing_quotas.expires_at<=clock_timestamp()
              THEN clock_timestamp()+seconds*interval '1 second' ELSE coleta_pairing_quotas.expires_at END
          WHERE coleta_pairing_quotas.expires_at<=clock_timestamp() OR coleta_pairing_quotas.used<maximum
          RETURNING used INTO amount;
          RETURN amount IS NOT NULL;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION coleta_pairing_limit(text,integer,integer) FROM PUBLIC")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
      GRANT EXECUTE ON FUNCTION coleta_pairing_limit(text,integer,integer) TO bancaemdia_app;
    END IF; END $$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM coleta_instalacoes WHERE pareado_em IS NOT NULL)
        THEN RAISE EXCEPTION 'preserve installation inventory before downgrade'; END IF; END $$""")
    op.execute("DROP FUNCTION coleta_pairing_limit(text,integer,integer)")
    op.drop_table("coleta_pairing_quotas")
    op.drop_table("coleta_pairing_codes")
    op.drop_table("coleta_instalacoes")
    # Revocation is deliberately irreversible. Rolling back never resurrects secrets.
