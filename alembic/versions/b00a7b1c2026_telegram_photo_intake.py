"""Durable Telegram photo extraction and source metadata.

Revision ID: b00a7b1c2026
Revises: a99a7b1c2026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b00a7b1c2026"
down_revision: str | Sequence[str] | None = "a99a7b1c2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rascunhos_aposta",
        sa.Column(
            "source_metadata_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.add_column(
        "rascunhos_aposta",
        sa.Column(
            "coupon_candidates_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "rascunhos_aposta",
        sa.Column("extraction_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "rascunhos_aposta",
        sa.Column(
            "extraction_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "rascunhos_aposta", sa.Column("extraction_lease_until", sa.DateTime(timezone=True))
    )
    op.add_column("rascunhos_aposta", sa.Column("extraction_lease_token", sa.String(32)))
    op.add_column("rascunhos_aposta", sa.Column("extraction_error_code", sa.String(32)))
    op.add_column(
        "rascunhos_aposta", sa.Column("extraction_completed_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_rascunhos_aposta_extraction_attempts", "rascunhos_aposta", "extraction_attempts >= 0"
    )
    op.create_index(
        "idx_rascunhos_aposta_extraction_due",
        "rascunhos_aposta",
        ["extraction_next_attempt_at", "id"],
        postgresql_where=sa.text(
            "extraction_completed_at IS NULL AND "
            "status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION','AWAITING_CONFIRMATION')"
        ),
    )
    # The transport can discover due drafts; all mutations still need the linked tenant GUC.
    op.execute(
        "CREATE POLICY rascunhos_aposta_transport_read ON rascunhos_aposta FOR SELECT "
        "USING (current_setting('app.telegram_transport', true) = 'on')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY rascunhos_aposta_transport_read ON rascunhos_aposta")
    op.drop_index("idx_rascunhos_aposta_extraction_due", table_name="rascunhos_aposta")
    op.drop_constraint("ck_rascunhos_aposta_extraction_attempts", "rascunhos_aposta")
    for column in (
        "extraction_completed_at",
        "extraction_error_code",
        "extraction_lease_token",
        "extraction_lease_until",
        "extraction_next_attempt_at",
        "extraction_attempts",
        "coupon_candidates_json",
        "source_metadata_json",
    ):
        op.drop_column("rascunhos_aposta", column)
