"""003_coleta_token_lookup

Revision ID: 3c1f0a9d7b21
Revises: faf3ac7240ef
Create Date: 2026-09-15 18:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c1f0a9d7b21"
down_revision: str | None = "faf3ac7240ef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOKEN_OFERECIDO = "NULLIF(current_setting('app.coleta_token_hash', true), '')"


def upgrade() -> None:
    # A extensão chega sem sessão e só com o token, então a pergunta é "de quem é este token?",
    # antes de haver usuário atual. Quem apresenta o hash lê só a linha desse hash.
    op.execute(
        "CREATE POLICY coleta_token_por_hash ON coleta_token FOR SELECT"
        f" USING (token_hash = {TOKEN_OFERECIDO})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY coleta_token_por_hash ON coleta_token")
