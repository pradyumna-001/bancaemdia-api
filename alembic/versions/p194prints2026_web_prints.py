"""Durable web print intake and forced tenant isolation."""

from typing import cast

from alembic import op
from sqlalchemy import Table

revision = "p194prints2026"
down_revision = "s183filter2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from bancaemdia.models.web_print import PrintAsset, PrintIntent, PrintItem

    bind = op.get_bind()
    for model in (PrintIntent, PrintAsset, PrintItem):
        cast(Table, model.__table__).create(bind)
        table = model.__tablename__
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON {table} USING (usuario_id = nullif(current_setting('app.current_user_id',true),'')::bigint) WITH CHECK (usuario_id = nullif(current_setting('app.current_user_id',true),'')::bigint)"
        )
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
        )
    op.execute(
        "CREATE INDEX print_assets_due ON print_assets(status, lease_until) WHERE status IN ('accepted','processing')"
    )
    op.execute("""CREATE FUNCTION prints_pending(lim integer) RETURNS TABLE(usuario_id bigint,id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT a.usuario_id,a.id FROM print_assets a WHERE a.status='accepted'
      OR (a.status='processing' AND a.lease_until < clock_timestamp())
      ORDER BY a.criado_em LIMIT least(greatest(lim,0),100) $$""")
    op.execute("""CREATE FUNCTION prints_purge() RETURNS void
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      UPDATE print_assets SET ciphertext=NULL WHERE ciphertext IS NOT NULL AND media_expires_at <= clock_timestamp();
      UPDATE print_assets a SET reading=NULL, results=NULL WHERE (reading IS NOT NULL OR results IS NOT NULL)
      AND NOT EXISTS (SELECT 1 FROM print_items i JOIN print_intents n ON n.usuario_id=i.usuario_id AND n.id=i.intent_id
        WHERE i.asset_id=a.id AND i.usuario_id=a.usuario_id AND n.expires_at > clock_timestamp()); $$""")
    op.execute("""DO $guard$ DECLARE definition text; BEGIN
      SELECT pg_get_functiondef('public.billing_require_write()'::regprocedure) INTO definition;
      IF strpos(definition,'web_print_retention')=0 THEN
        definition := replace(definition,'uid :=',$rules$
          -- web_print_retention: narrowly bounded queue denial/erasure, never new business content.
          IF TG_TABLE_NAME IN ('print_intents','print_items','print_assets') AND TG_OP='DELETE'
             AND NOT EXISTS(SELECT 1 FROM usuarios WHERE id=OLD.usuario_id) THEN RETURN OLD; END IF;
          IF TG_TABLE_NAME='print_assets' AND TG_OP='UPDATE' THEN
            IF NEW.status='failed' AND OLD.status IN ('accepted','processing')
              AND NEW.error_code IN ('account_read_only','media_expired')
              AND (NEW.error_code='account_read_only' OR OLD.media_expires_at <= clock_timestamp())
              AND (to_jsonb(NEW)-ARRAY['status','error_code','retryable','lease_token','lease_until'])
                = (to_jsonb(OLD)-ARRAY['status','error_code','retryable','lease_token','lease_until'])
            THEN RETURN NEW; END IF;
            IF (to_jsonb(NEW)-ARRAY['ciphertext','reading','results'])
                 = (to_jsonb(OLD)-ARRAY['ciphertext','reading','results'])
              AND (NEW.ciphertext IS NOT DISTINCT FROM OLD.ciphertext
                OR (NEW.ciphertext IS NULL AND OLD.media_expires_at <= clock_timestamp()))
              AND ((NEW.reading IS NOT DISTINCT FROM OLD.reading AND NEW.results IS NOT DISTINCT FROM OLD.results)
                OR (NEW.reading IS NULL AND NEW.results IS NULL AND NOT EXISTS (
                  SELECT 1 FROM print_items i JOIN print_intents n ON n.usuario_id=i.usuario_id AND n.id=i.intent_id
                  WHERE i.usuario_id=OLD.usuario_id AND i.asset_id=OLD.id AND n.expires_at > clock_timestamp())))
            THEN RETURN NEW; END IF;
          END IF;
          uid :=
        $rules$);
        EXECUTE definition;
      END IF;
    END $guard$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM print_intents) OR EXISTS(SELECT 1 FROM print_items) OR EXISTS(SELECT 1 FROM print_assets)
      THEN RAISE EXCEPTION 'preserve web print identities before downgrade'; END IF;
    END $$""")
    op.execute("DROP FUNCTION prints_purge()")
    op.execute("DROP FUNCTION prints_pending(integer)")
    for table in ("print_items", "print_assets", "print_intents"):
        op.drop_table(table)
