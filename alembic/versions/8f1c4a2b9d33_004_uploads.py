"""004_uploads

Revision ID: 8f1c4a2b9d33
Revises: 3c1f0a9d7b21
Create Date: 2026-09-19 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f1c4a2b9d33"
down_revision: str | None = "3c1f0a9d7b21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
JOB_OFERECIDO = "NULLIF(current_setting('app.upload_job_id', true), '')::uuid"
POR_USUARIO = ("uploads", "upload_bilhetes", "upload_arquivos")
ESTADOS_DO_UPLOAD = "'pending', 'processing', 'completed', 'failed'"
ESTADOS_DO_BILHETE = "'PENDENTE', 'LIDO', 'FALHOU', 'IGNORADO', 'TETO'"


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "job_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("total_messages", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("estimated_bets", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "estimated_cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("bets_processed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bets_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("erro", sa.String(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("concluido_em", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({ESTADOS_DO_UPLOAD})", name="ck_uploads_status"),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("idx_uploads_usuario", "uploads", ["usuario_id", "criado_em"], unique=False)
    op.create_table(
        "upload_bilhetes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("midia_hash", sa.String(), nullable=True),
        sa.Column("estado", sa.String(), server_default=sa.text("'PENDENTE'"), nullable=False),
        sa.Column("apostas", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "custo_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("enfileirado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(f"estado IN ({ESTADOS_DO_BILHETE})", name="ck_upload_bilhetes_estado"),
        sa.ForeignKeyConstraint(["upload_id"], ["uploads.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_id", "chat_id", "message_id", name="uq_upload_bilhetes_mensagem"
        ),
    )
    op.create_index(
        "idx_upload_bilhetes_usuario", "upload_bilhetes", ["usuario_id", "criado_em"], unique=False
    )
    op.create_table(
        "upload_arquivos",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("conteudo", sa.LargeBinary(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["upload_id"], ["uploads.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_upload_arquivos_upload", "upload_arquivos", ["upload_id"], unique=True)
    op.create_table(
        "midia_arquivos",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("conteudo", sa.LargeBinary(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["hash"], ["midias.hash"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    # A foto do Telegram já é JPEG: tentar comprimir de novo gasta processador e não encolhe nada.
    for tabela in ("upload_arquivos", "midia_arquivos"):
        op.execute(f"ALTER TABLE {tabela} ALTER COLUMN conteudo SET STORAGE EXTERNAL")

    op.add_column("mensagens", sa.Column("editada_em", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mensagens", sa.Column("midia_hash", sa.String(), nullable=True))
    op.create_index("idx_mensagens_midia_hash", "mensagens", ["midia_hash"], unique=False)

    for tabela in POR_USUARIO:
        op.execute(f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabela} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tabela}_por_usuario ON {tabela} FOR ALL"
            f" USING (usuario_id = {USUARIO_ATUAL}) WITH CHECK (usuario_id = {USUARIO_ATUAL})"
        )
    # O aviso de "terminei" chega do trabalhador sem usuário: quem apresenta o job_id enxerga só
    # aquela linha, como a política do token da extensão (003).
    op.execute(
        "CREATE POLICY uploads_por_job ON uploads FOR ALL"
        f" USING (job_id = {JOB_OFERECIDO}) WITH CHECK (job_id = {JOB_OFERECIDO})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY uploads_por_job ON uploads")
    for tabela in reversed(POR_USUARIO):
        op.execute(f"DROP POLICY {tabela}_por_usuario ON {tabela}")
        op.execute(f"ALTER TABLE {tabela} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabela} DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_mensagens_midia_hash", table_name="mensagens")
    op.drop_column("mensagens", "midia_hash")
    op.drop_column("mensagens", "editada_em")
    op.drop_table("midia_arquivos")
    op.drop_index("idx_upload_arquivos_upload", table_name="upload_arquivos")
    op.drop_table("upload_arquivos")
    op.drop_index("idx_upload_bilhetes_usuario", table_name="upload_bilhetes")
    op.drop_table("upload_bilhetes")
    op.drop_index("idx_uploads_usuario", table_name="uploads")
    op.drop_table("uploads")
