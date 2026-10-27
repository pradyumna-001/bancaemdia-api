"""Unit-value periods written by the site keep their client identity and original receipt."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "u210unit2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("unidades", sa.Column("chave_idempotencia", sa.String(200)))
    op.add_column("unidades", sa.Column("requisicao_hash", sa.String(64)))
    op.add_column("unidades", sa.Column("recibo_json", JSONB()))
    # Vigência antiga (migração do monólito) não tem identidade; a criada pelo site tem as três.
    op.create_check_constraint(
        "ck_unidades_idempotencia",
        "unidades",
        "(chave_idempotencia IS NULL AND requisicao_hash IS NULL AND recibo_json IS NULL)"
        " OR (chave_idempotencia IS NOT NULL AND requisicao_hash IS NOT NULL"
        " AND recibo_json IS NOT NULL)",
    )
    op.create_index(
        "uq_unidades_usuario_chave",
        "unidades",
        ["usuario_id", "chave_idempotencia"],
        unique=True,
        postgresql_where=sa.text("chave_idempotencia IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM unidades WHERE chave_idempotencia IS NOT NULL)"
        " THEN RAISE EXCEPTION 'unit periods with client identity exist: roll back application,"
        " retain schema'; END IF; END $$"
    )
    op.drop_index("uq_unidades_usuario_chave", table_name="unidades")
    op.drop_constraint("ck_unidades_idempotencia", "unidades", type_="check")
    for column in ("recibo_json", "requisicao_hash", "chave_idempotencia"):
        op.drop_column("unidades", column)
