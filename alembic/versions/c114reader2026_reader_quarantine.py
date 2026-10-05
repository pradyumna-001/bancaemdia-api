"""Reader schema drift quarantine, isolated by owner.

Revision ID: c114reader2026
Revises: a9d6e3f1c210
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c114reader2026"
down_revision: str | None = "a9d6e3f1c210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reader_quarantine",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("capture_sha256", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("envelope", JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "usuario_id",
            "capture_sha256",
            "error_code",
            "reason",
            name="uq_reader_quarantine_capture",
        ),
    )
    op.create_index(
        "idx_reader_quarantine_owner_date", "reader_quarantine", ["usuario_id", "created_at"]
    )
    op.execute("ALTER TABLE reader_quarantine ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reader_quarantine FORCE ROW LEVEL SECURITY")
    uid = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
    op.execute(
        f"CREATE POLICY reader_quarantine_read ON reader_quarantine FOR SELECT USING (usuario_id = {uid})"
    )
    op.execute(
        f"CREATE POLICY reader_quarantine_insert ON reader_quarantine FOR INSERT WITH CHECK (usuario_id = {uid})"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM reader_quarantine) THEN RAISE EXCEPTION 'reader quarantine evidence exists: roll back application, retain schema'; END IF; END $$"
    )
    op.drop_table("reader_quarantine")
