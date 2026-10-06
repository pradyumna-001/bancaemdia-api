"""007_caixa_idempotencia

Revision ID: c7b1e9a42d60
Revises: a4e7d2c9f103
Create Date: 2026-09-22 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7b1e9a42d60"
down_revision: str | None = "a4e7d2c9f103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"


def upgrade() -> None:
    op.create_table(
        "movimento_requisicoes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("chave_idempotencia", sa.String(length=200), nullable=False),
        sa.Column("requisicao_hash", sa.String(length=64), nullable=False),
        sa.Column("resposta_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "chave_idempotencia",
            name="uq_movimento_requisicoes_usuario_chave",
        ),
    )
    op.execute("ALTER TABLE movimento_requisicoes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE movimento_requisicoes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY movimento_requisicoes_por_usuario ON movimento_requisicoes FOR ALL"
        f" USING (usuario_id = {USUARIO_ATUAL}) WITH CHECK (usuario_id = {USUARIO_ATUAL})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY movimento_requisicoes_por_usuario ON movimento_requisicoes")
    op.execute("ALTER TABLE movimento_requisicoes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE movimento_requisicoes DISABLE ROW LEVEL SECURITY")
    op.drop_table("movimento_requisicoes")
