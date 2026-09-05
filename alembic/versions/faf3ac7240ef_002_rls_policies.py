"""002_rls_policies

Revision ID: faf3ac7240ef
Revises: be7d60cb5437
Create Date: 2026-09-05 12:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "faf3ac7240ef"
down_revision: str | None = "be7d60cb5437"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
POR_USUARIO = (
    "bancas",
    "contas_casa",
    "unidades",
    "movimentos",
    "apostas",
    "eventos",
    "revisao_pendente",
    "coletas_casa",
    "coleta_token",
    "chamadas_ia",
)
POLITICAS_DE_USUARIOS = (
    ("usuarios_leitura", "FOR SELECT USING (true)"),
    ("usuarios_cadastro", "FOR INSERT WITH CHECK (true)"),
    (
        "usuarios_alteracao",
        f"FOR UPDATE USING (id = {USUARIO_ATUAL}) WITH CHECK (id = {USUARIO_ATUAL})",
    ),
    ("usuarios_exclusao", f"FOR DELETE USING (id = {USUARIO_ATUAL})"),
)


def upgrade() -> None:
    op.execute("ALTER TABLE usuarios ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usuarios FORCE ROW LEVEL SECURITY")
    for nome, regra in POLITICAS_DE_USUARIOS:
        op.execute(f"CREATE POLICY {nome} ON usuarios {regra}")
    for tabela in POR_USUARIO:
        op.execute(f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabela} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tabela}_por_usuario ON {tabela} FOR ALL"
            f" USING (usuario_id = {USUARIO_ATUAL}) WITH CHECK (usuario_id = {USUARIO_ATUAL})"
        )


def downgrade() -> None:
    for tabela in reversed(POR_USUARIO):
        op.execute(f"DROP POLICY {tabela}_por_usuario ON {tabela}")
        op.execute(f"ALTER TABLE {tabela} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabela} DISABLE ROW LEVEL SECURITY")
    for nome, _ in reversed(POLITICAS_DE_USUARIOS):
        op.execute(f"DROP POLICY {nome} ON usuarios")
    op.execute("ALTER TABLE usuarios NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usuarios DISABLE ROW LEVEL SECURITY")
