"""011_catalogo_casas: exact-host evidence, publications and operator authorization.

Revision ID: c113catalog2026
Revises: a9d6e3f1c210
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c113catalog2026"
down_revision: str | None = "a9d6e3f1c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
UID = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
GLOBAL = ("casa_dominios", "catalogo_fontes", "catalogo_snapshots")
APPEND_ONLY = (
    "catalogo_snapshots",
    "catalogo_publicacoes",
    "catalogo_confirmacoes",
    "catalogo_auditoria",
)


def pk() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), primary_key=True)


def owner() -> sa.Column:
    return sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False)


def upgrade() -> None:
    op.create_table("catalogo_operadores", pk(), owner(), sa.UniqueConstraint("usuario_id"))
    op.create_table(
        "casa_dominios",
        pk(),
        sa.Column("marca", sa.String(120), nullable=False),
        sa.Column("hostname", sa.String(253), nullable=False),
        sa.Column("tecnico", JSONB(), nullable=False),
        sa.Column("evidencias", JSONB(), nullable=False),
        sa.UniqueConstraint("marca", "hostname", name="uq_casa_dominio_marca_host"),
        sa.CheckConstraint(
            "hostname = lower(hostname) AND hostname !~ '[*/:@?#]'", name="ck_catalogo_host_exato"
        ),
    )
    op.create_table(
        "catalogo_fontes",
        pk(),
        sa.Column("source_key", sa.String(80), nullable=False, unique=True),
        sa.Column("jurisdiction", sa.String(10), nullable=False),
        sa.Column("dados", JSONB(), nullable=False),
    )
    op.create_table(
        "catalogo_snapshots",
        pk(),
        sa.Column(
            "source_key", sa.String(80), sa.ForeignKey("catalogo_fontes.source_key"), nullable=False
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("consultado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fonte", JSONB(), nullable=False),
        sa.Column("raw_base64", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "source_key", "snapshot_sha256", "consultado_em", name="uq_catalogo_snapshot_fonte_hash"
        ),
    )
    op.create_table(
        "catalogo_publicacoes",
        pk(),
        sa.Column("ambiente", sa.String(40), nullable=False),
        sa.Column("emitido_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("envelope", JSONB(), nullable=False),
        sa.Column("etag", sa.String(64), nullable=False, unique=True),
    )
    op.create_table(
        "catalogo_confirmacoes",
        pk(),
        owner(),
        sa.Column("marca", sa.String(120), nullable=False),
        sa.Column("hostname", sa.String(253), nullable=False),
        sa.Column("confirmado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "usuario_id", "marca", "hostname", "evidence_sha256", name="uq_catalogo_confirmacao"
        ),
    )
    op.create_table(
        "catalogo_auditoria",
        pk(),
        owner(),
        sa.Column("ocorrido_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acao", sa.String(40), nullable=False),
        sa.Column("dados", JSONB(), nullable=False),
    )
    # Only the database owner provisions operators. The NOBYPASSRLS API role cannot self-grant.
    op.execute("ALTER TABLE catalogo_operadores ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY catalogo_operador_proprio ON catalogo_operadores FOR SELECT USING (usuario_id = {UID})"
    )
    op.execute(f"""CREATE FUNCTION catalogo_is_operator() RETURNS boolean LANGUAGE sql STABLE
        SECURITY INVOKER SET search_path = pg_catalog, public AS $$
        SELECT EXISTS (SELECT 1 FROM public.catalogo_operadores o JOIN public.usuarios u ON u.id = o.usuario_id WHERE o.usuario_id = {UID} AND u.ativo) $$""")
    for table in (*GLOBAL, "catalogo_publicacoes", "catalogo_confirmacoes", "catalogo_auditoria"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table in GLOBAL:
        op.execute(
            f"CREATE POLICY {table}_operador ON {table} FOR ALL USING (catalogo_is_operator()) WITH CHECK (catalogo_is_operator())"
        )
    op.execute(
        f"CREATE POLICY catalogo_publicacao_leitura ON catalogo_publicacoes FOR SELECT USING (EXISTS (SELECT 1 FROM usuarios WHERE id = {UID} AND ativo))"
    )
    op.execute(
        "CREATE POLICY catalogo_publicacao_escrita ON catalogo_publicacoes FOR INSERT WITH CHECK (catalogo_is_operator())"
    )
    op.execute(
        f"CREATE POLICY catalogo_confirmacao_leitura ON catalogo_confirmacoes FOR SELECT USING (usuario_id = {UID} OR catalogo_is_operator())"
    )
    op.execute(
        f"CREATE POLICY catalogo_confirmacao_escrita ON catalogo_confirmacoes FOR INSERT WITH CHECK (usuario_id = {UID})"
    )
    op.execute(
        f"CREATE POLICY catalogo_auditoria_leitura ON catalogo_auditoria FOR SELECT USING (usuario_id = {UID} OR catalogo_is_operator())"
    )
    op.execute(
        f"CREATE POLICY catalogo_auditoria_escrita ON catalogo_auditoria FOR INSERT WITH CHECK (usuario_id = {UID} AND (catalogo_is_operator() OR acao = 'manual_confirmed'))"
    )
    op.execute("""CREATE FUNCTION catalogo_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'catalog history is append-only'; END $$""")
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION catalogo_append_only()"
        )


def downgrade() -> None:
    # Offline SQL remains reviewable; an online rollback refuses destructive audit loss.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM catalogo_snapshots) OR EXISTS (SELECT 1 FROM catalogo_publicacoes)
          OR EXISTS (SELECT 1 FROM catalogo_confirmacoes) OR EXISTS (SELECT 1 FROM catalogo_auditoria)
        THEN RAISE EXCEPTION 'catalog audit data exists: roll back application, retain schema'; END IF;
        END $$""")
    for table in reversed((
        *GLOBAL,
        "catalogo_publicacoes",
        "catalogo_confirmacoes",
        "catalogo_auditoria",
    )):
        op.drop_table(table)
    op.execute("DROP FUNCTION catalogo_append_only()")
    op.execute("DROP FUNCTION catalogo_is_operator()")
    op.drop_table("catalogo_operadores")
