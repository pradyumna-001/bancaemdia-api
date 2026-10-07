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
    mapping = run_path(str(Path(__file__).with_name("g93tenant2026_fail_closed_tenant_guards.py")))[
        "TENANT_COLUMNS"
    ]
    mapping.update(
        dict.fromkeys(
            (
                "aposta_consolidacoes",
                "cruzamento_candidatos",
                "cruzamento_entradas",
                "reader_quarantine",
                "reconciliacao_chunks",
                "coleta_entregas",
                "telegram_media",
            ),
            "usuario_id",
        )
    )
    values = ",".join(f"('{table}','{column}')" for table, column in mapping.items())
    op.execute(f"""CREATE OR REPLACE FUNCTION billing_install_write_guards() RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $billing$
      DECLARE item record; target regclass; BEGIN
        FOR item IN SELECT * FROM (VALUES {values}) AS mapping(table_name,tenant_column) LOOP
          target := to_regclass('public.'||item.table_name);
          IF target IS NULL THEN CONTINUE; END IF;
          IF NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=target
            AND attname=item.tenant_column AND NOT attisdropped AND atttypid='bigint'::regtype)
          THEN RAISE EXCEPTION 'billing tenant column invalid: %.%',item.table_name,item.tenant_column
            USING ERRCODE='P0402'; END IF;
          IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=target
            AND tgname='billing_write_guard' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.billing_require_write()',item.table_name);
          END IF;
        END LOOP;
      END $billing$""")
    op.execute("REVOKE ALL ON FUNCTION billing_install_write_guards() FROM PUBLIC")
    op.execute("""DO $control$ DECLARE definition text; BEGIN
      SELECT pg_get_functiondef('public.billing_require_write()'::regprocedure) INTO definition;
      IF strpos(definition,'collection_pause_control')=0 THEN
        IF strpos(definition,'-- Cancellation and deferred-queue metadata')=0
        THEN RAISE EXCEPTION 'unknown billing guard contract'; END IF;
        definition := replace(definition,'-- Cancellation and deferred-queue metadata are control operations.', $inject$
          -- collection_pause_control: no envelope, identity, status or financial edit.
          IF TG_TABLE_NAME='coleta_entregas' AND TG_OP='UPDATE' THEN
            IF OLD.status='pending' AND NEW.status='pending' AND NEW.reason='account_read_only'
              AND (to_jsonb(NEW)-'reason')=(to_jsonb(OLD)-'reason') THEN RETURN NEW; END IF;
          END IF;
          IF TG_TABLE_NAME='telegram_media' AND TG_OP='DELETE' THEN
            IF EXISTS(SELECT 1 FROM public.rascunhos_aposta WHERE id=OLD.draft_id
              AND usuario_id=uid AND status IN ('CONFIRMED','CANCELLED','FAILED'))
            THEN RETURN OLD; END IF;
          END IF;
          -- Cancellation and deferred-queue metadata are control operations.
        $inject$);
        EXECUTE definition;
      END IF;
    END $control$""")
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    # Keep financial projection and privacy guards while parent branches remain.
    pass
