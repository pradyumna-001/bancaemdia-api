"""Durable web print intake and forced tenant isolation."""

from alembic import op

revision = "p194prints2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from bancaemdia.models.web_print import PrintAsset, PrintIntent, PrintItem

    bind = op.get_bind()
    for model in (PrintIntent, PrintAsset, PrintItem):
        model.__table__.create(bind)
        table = model.__tablename__
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON {table} USING (usuario_id = nullif(current_setting('app.current_user_id',true),'')::bigint) WITH CHECK (usuario_id = nullif(current_setting('app.current_user_id',true),'')::bigint)"
        )
        op.execute(
            f"CREATE TRIGGER billing_print_insert BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
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


def downgrade() -> None:
    op.execute("DROP FUNCTION prints_purge()")
    op.execute("DROP FUNCTION prints_pending(integer)")
    for table in ("print_items", "print_assets", "print_intents"):
        op.drop_table(table)
