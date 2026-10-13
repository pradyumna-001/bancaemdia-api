"""Opaque revisions across all writers and private result receipts. Preserve audit on rollback."""

from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "r198result2026"
down_revision = "s196preview2026"
branch_labels = None
depends_on = None


def _event_types(undo: bool) -> None:
    previous = run_path(str(Path(__file__).with_name("c110fact2026_aposta_consolidacoes.py")))
    names = (*previous["EVENT_TYPES"], *(("RESULTADO_DESFEITO",) if undo else ()))
    op.drop_constraint("ck_eventos_tipo", "eventos", type_="check")
    op.create_check_constraint(
        "ck_eventos_tipo", "eventos", f"tipo IN ({','.join(map(repr, names))})"
    )


def upgrade() -> None:
    # Kept outside the legacy ORM projection to preserve its published record constructor.
    op.add_column(
        "apostas",
        sa.Column(
            "result_revision", UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
    )
    # Invalidation is metadata, not a new matching source. Otherwise a review update
    # would recursively invalidate the row, invoke matching and resolve itself again.
    op.execute("DROP TRIGGER matching_source_changed ON apostas")
    op.execute("""CREATE TRIGGER matching_source_changed AFTER UPDATE ON apostas FOR EACH ROW
      WHEN ((to_jsonb(OLD)-'result_revision') IS DISTINCT FROM (to_jsonb(NEW)-'result_revision'))
      EXECUTE FUNCTION matching_source_changed()""")
    op.execute("""CREATE FUNCTION result_revision_row() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN NEW.result_revision := gen_random_uuid(); RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER result_revision_row BEFORE UPDATE ON apostas FOR EACH ROW EXECUTE FUNCTION result_revision_row()"
    )
    op.execute("""CREATE FUNCTION result_revision_fact() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_ARGV[0]='eventos' THEN
          UPDATE apostas SET result_revision=gen_random_uuid() WHERE usuario_id=NEW.usuario_id AND chave=NEW.aposta_chave;
        ELSIF TG_ARGV[0]='aposta_consolidacoes' THEN
          UPDATE apostas SET result_revision=gen_random_uuid() WHERE usuario_id=NEW.usuario_id AND id IN (NEW.casa_aposta_id,NEW.telegram_aposta_id);
        ELSE
          -- Reviews have optional media/context identities; conservatively invalidate the owner.
          UPDATE apostas SET result_revision=gen_random_uuid() WHERE usuario_id=NEW.usuario_id;
        END IF;
        RETURN NEW;
      END $$""")
    for table in ("eventos", "aposta_consolidacoes", "revisao_pendente"):
        operations = "INSERT" if table == "eventos" else "INSERT OR UPDATE"
        op.execute(
            f"CREATE TRIGGER result_revision_fact AFTER {operations} ON {table} FOR EACH ROW EXECUTE FUNCTION result_revision_fact('{table}')"
        )
    op.create_table(
        "result_operations",
        sa.Column("usuario_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("aposta_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("before", JSONB(), nullable=False),
        sa.Column("receipt", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("kind IN ('result','undo')", name="ck_result_operation_kind"),
    )
    op.execute("ALTER TABLE result_operations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE result_operations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY result_owner ON result_operations FOR ALL
      USING(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
      WITH CHECK(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)""")
    op.execute(
        "CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON result_operations FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
    )
    op.execute("""CREATE FUNCTION result_operation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' AND OLD.usuario_id=NULLIF(current_setting('app.erase_user_data',true),'')::bigint
          AND OLD.usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'preserve result intention audit';
      END $$""")
    op.execute(
        "CREATE TRIGGER result_operation_immutable BEFORE UPDATE OR DELETE ON result_operations FOR EACH ROW EXECUTE FUNCTION result_operation_immutable()"
    )
    # Roles already installed in production inherit only the owned-table privileges.
    op.execute("""DO $$ DECLARE entry record; BEGIN
      FOR entry IN SELECT DISTINCT grantee::regrole::text AS role FROM aclexplode(
        (SELECT relacl FROM pg_class WHERE oid='public.apostas'::regclass))
        WHERE privilege_type='SELECT' AND grantee<>0 LOOP
        EXECUTE format('GRANT SELECT,INSERT,DELETE ON result_operations TO %I',entry.role);
      END LOOP; END $$""")
    _event_types(True)


def downgrade() -> None:
    op.execute("SET LOCAL row_security=off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM result_operations) OR EXISTS(
      SELECT 1 FROM eventos WHERE tipo='RESULTADO_DESFEITO') THEN
      RAISE EXCEPTION 'preserve result intentions and reversal audit before downgrade'; END IF; END $$""")
    op.drop_table("result_operations")
    op.execute("DROP FUNCTION result_operation_immutable()")
    for table in ("eventos", "aposta_consolidacoes", "revisao_pendente"):
        op.execute(f"DROP TRIGGER result_revision_fact ON {table}")
    op.execute("DROP FUNCTION result_revision_fact()")
    op.execute("DROP TRIGGER result_revision_row ON apostas")
    op.execute("DROP FUNCTION result_revision_row()")
    op.drop_column("apostas", "result_revision")
    op.execute("DROP TRIGGER matching_source_changed ON apostas")
    op.execute("""CREATE TRIGGER matching_source_changed AFTER UPDATE ON apostas FOR EACH ROW
      WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION matching_source_changed()""")
    _event_types(False)
