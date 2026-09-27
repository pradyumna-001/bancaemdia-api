"""Durable Telegram update inbox and tenant-owned reply outbox.

Revision ID: f98a7b1c2026
Revises: e97a7b1c2026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f98a7b1c2026"
down_revision: str | Sequence[str] | None = "e97a7b1c2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
TRANSPORT = "current_setting('app.telegram_transport', true) = 'on'"


def upgrade() -> None:
    op.create_table(
        "telegram_inbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger()),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("payload_ciphertext", sa.LargeBinary()),
        sa.Column("status", sa.String(16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(40)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('PENDING','DONE','DLQ')", name="ck_telegram_inbox_status"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 8", name="ck_telegram_inbox_attempts"),
    )
    op.create_index(
        "idx_telegram_inbox_due",
        "telegram_inbox",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_table(
        "telegram_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("last_error_code", sa.String(40)),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','DLQ')", name="ck_telegram_outbox_status"
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 8", name="ck_telegram_outbox_attempts"),
        sa.UniqueConstraint("usuario_id", "idempotency_key", name="uq_telegram_outbox_key"),
    )
    op.create_index(
        "idx_telegram_outbox_due",
        "telegram_outbox",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('PENDING','SENDING')"),
    )
    for table in ("telegram_inbox", "telegram_outbox"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY telegram_inbox_transport ON telegram_inbox FOR ALL USING ({TRANSPORT}) WITH CHECK ({TRANSPORT})"
    )
    op.execute(
        f"CREATE POLICY telegram_outbox_owner ON telegram_outbox FOR ALL USING (usuario_id = {TENANT} OR {TRANSPORT}) WITH CHECK (usuario_id = {TENANT} OR {TRANSPORT})"
    )
    op.execute(
        "CREATE TRIGGER audit_telegram_outbox_write AFTER INSERT OR UPDATE OR DELETE ON telegram_outbox FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_telegram_outbox_write ON telegram_outbox")
    op.execute("DROP POLICY telegram_outbox_owner ON telegram_outbox")
    op.execute("DROP POLICY telegram_inbox_transport ON telegram_inbox")
    op.drop_index("idx_telegram_outbox_due", table_name="telegram_outbox")
    op.drop_table("telegram_outbox")
    op.drop_index("idx_telegram_inbox_due", table_name="telegram_inbox")
    op.drop_table("telegram_inbox")
