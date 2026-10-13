"""Private spreadsheet previews and durable atomic application identities."""

from alembic import op
from sqlalchemy import text

from bancaemdia.models.sheet_import import SheetConfirmation, SheetPreview, SheetSource

revision = "s196preview2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None
TABLES = (SheetSource.__table__, SheetPreview.__table__, SheetConfirmation.__table__)


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=False)
        name = table.name
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        owner = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
        op.execute(
            f"CREATE POLICY {name}_por_usuario ON {name} FOR ALL USING (usuario_id = {owner}) WITH CHECK (usuario_id = {owner})"
        )
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
        )
    op.execute("""DO $patch$ DECLARE definition text; BEGIN
      SELECT pg_get_functiondef('public.billing_require_write()'::regprocedure) INTO definition;
      IF strpos(definition,'-- Cancellation and deferred-queue metadata')=0
        THEN RAISE EXCEPTION 'unknown billing guard contract'; END IF;
      IF strpos(definition,'sheet_import_retention')=0 THEN
        definition := replace(definition,'-- Cancellation and deferred-queue metadata are control operations.', $inject$
          -- sheet_import_retention: only removal of expired private material.
          IF TG_TABLE_NAME IN ('sheet_sources','sheet_previews','sheet_confirmations') THEN
            IF TG_OP='DELETE' AND NOT EXISTS(SELECT 1 FROM public.usuarios WHERE id=uid)
              THEN RETURN OLD; END IF;
            IF TG_OP='UPDATE' AND TG_TABLE_NAME='sheet_previews' THEN
              IF OLD.expires_at<=clock_timestamp() AND NEW.private_data IS NULL
               AND (to_jsonb(NEW)-'private_data')=(to_jsonb(OLD)-'private_data')
                THEN RETURN NEW; END IF;
            END IF;
            IF TG_OP='UPDATE' AND TG_TABLE_NAME='sheet_confirmations' THEN
              IF OLD.expires_at<=clock_timestamp() AND NEW.private_result IS NULL
               AND (to_jsonb(NEW)-'private_result')=(to_jsonb(OLD)-'private_result')
                THEN RETURN NEW; END IF;
            END IF;
          END IF;
          -- Cancellation and deferred-queue metadata are control operations.
        $inject$);
        EXECUTE definition;
      END IF;
    END $patch$""")
    op.execute("""CREATE FUNCTION sheet_import_purge() RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE previews integer; confirmations integer; BEGIN
        UPDATE public.sheet_previews SET private_data=NULL
          WHERE expires_at<=clock_timestamp() AND private_data IS NOT NULL;
        GET DIAGNOSTICS previews=ROW_COUNT;
        UPDATE public.sheet_confirmations SET private_result=NULL
          WHERE expires_at<=clock_timestamp() AND private_result IS NOT NULL;
        GET DIAGNOSTICS confirmations=ROW_COUNT;
        RETURN previews+confirmations;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION sheet_import_purge() FROM PUBLIC")
    op.execute(
        "CREATE INDEX sheet_previews_expiry ON sheet_previews(expires_at) WHERE private_data IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX sheet_confirmations_expiry ON sheet_confirmations(expires_at) WHERE private_result IS NOT NULL"
    )


def downgrade() -> None:
    if not op.get_context().as_sql:
        for table in TABLES:
            if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table.name})")):
                raise RuntimeError("spreadsheet identities cannot be removed while populated")
    op.execute("DROP FUNCTION sheet_import_purge()")
    for table in reversed(TABLES):
        table.drop(op.get_bind(), checkfirst=False)
