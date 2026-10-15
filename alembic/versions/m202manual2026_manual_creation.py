"""Atomic immutable manual-creation receipts with tenant RLS and retained identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "m202manual2026"
down_revision = "j6main2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No FK to the bet: a correction or later physical deletion must not erase
    # proof of the original creation or permit resurrection with the same UUID.
    op.create_table(
        "manual_creation_operations",
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("receipt", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("ALTER TABLE manual_creation_operations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE manual_creation_operations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY manual_creation_owner ON manual_creation_operations FOR ALL
      USING(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
      WITH CHECK(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)""")
    op.execute("""CREATE FUNCTION manual_creation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' AND OLD.usuario_id::text=current_setting('app.erase_user_data',true)
          AND OLD.usuario_id::text=current_setting('app.current_user_id',true)
          THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'immutable manual creation receipt' USING ERRCODE='P0001';
      END $$""")
    op.execute(
        "CREATE TRIGGER manual_creation_immutable BEFORE UPDATE OR DELETE ON manual_creation_operations FOR EACH ROW EXECUTE FUNCTION manual_creation_immutable()"
    )
    op.execute(
        "CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON manual_creation_operations FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
    )
    op.execute("""DO $$ DECLARE entry record; BEGIN
      FOR entry IN SELECT DISTINCT grantee::regrole::text AS role FROM aclexplode(
        (SELECT relacl FROM pg_class WHERE oid='public.apostas'::regclass))
        WHERE privilege_type='SELECT' AND grantee<>0 LOOP
        EXECUTE format('GRANT SELECT,INSERT,DELETE ON manual_creation_operations TO %I',entry.role);
      END LOOP; END $$""")


def downgrade() -> None:
    op.execute("SET LOCAL row_security=off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM manual_creation_operations) THEN
      RAISE EXCEPTION 'preserve manual creation identities before downgrade'; END IF; END $$""")
    op.drop_table("manual_creation_operations")
    op.execute("DROP FUNCTION manual_creation_immutable()")
