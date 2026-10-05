"""Recognize user-confirmed Telegram drafts as a distinct bet origin.

Revision ID: b01a7b1c2026
Revises: b00a7b1c2026
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b01a7b1c2026"
down_revision: str | Sequence[str] | None = "b00a7b1c2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORIGINS = "origem IN ('telegram', 'print', 'manual', 'planilha', 'casa')"
NEW_ORIGINS = "origem IN ('telegram', 'telegram_bot', 'print', 'manual', 'planilha', 'casa')"


def upgrade() -> None:
    op.drop_constraint("ck_apostas_origem", "apostas", type_="check")
    op.create_check_constraint("ck_apostas_origem", "apostas", NEW_ORIGINS)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM apostas WHERE origem = 'telegram_bot') "
        "THEN RAISE EXCEPTION 'telegram_bot bets require an explicit data migration'; "
        "END IF; END $$"
    )
    op.drop_constraint("ck_apostas_origem", "apostas", type_="check")
    op.create_check_constraint("ck_apostas_origem", "apostas", OLD_ORIGINS)
