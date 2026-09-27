"""Resumable tenant-owned Telegram bet drafts, outside financial projections.

Revision ID: a99a7b1c2026
Revises: f98a7b1c2026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a99a7b1c2026"
down_revision: str | Sequence[str] | None = "f98a7b1c2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
ACTIVE = "status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION','AWAITING_CONFIRMATION')"


def upgrade() -> None:
    op.create_table(
        "rascunhos_aposta",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=False),
        sa.Column("media_reference_ciphertext", sa.LargeBinary()),
        sa.Column("media_hash", sa.String(64)),
        sa.Column("fields_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "field_meta_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "missing_fields_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'AWAITING_EXTRACTION'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("usuario_id", "id", name="uq_rascunhos_aposta_tenant_id"),
        sa.UniqueConstraint(
            "usuario_id",
            "telegram_chat_id",
            "telegram_message_id",
            name="uq_rascunhos_aposta_origem",
        ),
        sa.CheckConstraint(
            "status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION',"
            "'AWAITING_CONFIRMATION','CONFIRMED','CANCELLED','FAILED')",
            name="ck_rascunhos_aposta_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_rascunhos_aposta_version"),
    )
    op.create_index(
        "uq_rascunhos_aposta_chat_ativo",
        "rascunhos_aposta",
        ["usuario_id", "telegram_chat_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )
    op.create_table(
        "rascunho_correcoes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("rascunho_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger()),
        sa.Column("changes_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id", "rascunho_id"],
            ["rascunhos_aposta.usuario_id", "rascunhos_aposta.id"],
            name="fk_rascunho_correcoes_tenant_draft",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("rascunho_id", "version", name="uq_rascunho_correcoes_version"),
        sa.UniqueConstraint(
            "rascunho_id", "telegram_update_id", name="uq_rascunho_correcoes_update"
        ),
        sa.CheckConstraint("version > 1", name="ck_rascunho_correcoes_version"),
    )
    for table in ("rascunhos_aposta", "rascunho_correcoes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_por_usuario ON {table} FOR ALL "
            f"USING (usuario_id = {TENANT}) WITH CHECK (usuario_id = {TENANT})"
        )
        op.execute(
            f"CREATE TRIGGER audit_{table}_write AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
        )
        op.execute(
            f"CREATE TRIGGER active_{table}_write BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
        )
    op.execute(
        "CREATE TRIGGER rascunho_correcoes_no_update BEFORE UPDATE ON rascunho_correcoes "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER rascunho_correcoes_no_update ON rascunho_correcoes")
    for table in ("rascunho_correcoes", "rascunhos_aposta"):
        op.execute(f"DROP TRIGGER active_{table}_write ON {table}")
        op.execute(f"DROP TRIGGER audit_{table}_write ON {table}")
        op.execute(f"DROP POLICY {table}_por_usuario ON {table}")
    op.drop_table("rascunho_correcoes")
    op.drop_index("uq_rascunhos_aposta_chat_ativo", table_name="rascunhos_aposta")
    op.drop_table("rascunhos_aposta")
