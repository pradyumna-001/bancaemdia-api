"""Converge reviewed Telegram, analytics and canonical financial facts."""

from pathlib import Path
from runpy import run_path

from alembic import op

revision = "h6review2026"
down_revision = ("h5review2026", "h3review2026", "f158privacy2026")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION consolidation_sources() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('pareador:'||NEW.usuario_id,0));
        IF NOT EXISTS(SELECT 1 FROM apostas WHERE id=NEW.casa_aposta_id
          AND usuario_id=NEW.usuario_id AND origem='casa')
          OR NOT EXISTS(SELECT 1 FROM apostas WHERE id=NEW.telegram_aposta_id
          AND usuario_id=NEW.usuario_id AND origem IN ('telegram','print','telegram_bot'))
        THEN RAISE EXCEPTION 'invalid consolidation sources'; END IF;
        RETURN NEW;
      END $$""")
    run_path(str(Path(__file__).with_name("c110fact2026_aposta_consolidacoes.py")))["_dashboard"](
        True
    )
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    # Keep financial projection and privacy guards while parent branches remain.
    pass
