"""Reconcile real Print/Results code and protect review evidence across all writers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "r200review2026"
down_revision = ("r198result2026", "p194prints2026")
branch_labels = None
depends_on = None

EVIDENCE_TABLES = (
    "revisao_pendente",
    "contas_casa",
    "usos_conta_casa",
    "titulares",
    "bancas",
    "cruzamento_candidatos",
    "cruzamento_entradas",
    "aposta_consolidacoes",
    "print_assets",
)


def upgrade() -> None:
    # Metadata stays outside the legacy ORM records; an intermediate/SQL writer also
    # changes the revision, including a nominal update or an edit followed by restoration.
    op.execute("""CREATE FUNCTION review_evidence_revision() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN NEW.review_revision := gen_random_uuid(); RETURN NEW; END $$""")
    for table in EVIDENCE_TABLES:
        op.add_column(
            table,
            sa.Column(
                "review_revision",
                UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
        )
        op.execute(
            f"CREATE TRIGGER review_evidence_revision BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION review_evidence_revision()"
        )
    op.create_unique_constraint("uq_review_receipt_owner", "revisao_pendente", ["id", "usuario_id"])
    op.create_table(
        "review_operations",
        sa.Column("usuario_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("revisao_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("receipt", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["revisao_id", "usuario_id"],
            ["revisao_pendente.id", "revisao_pendente.usuario_id"],
            ondelete="CASCADE",
        ),
    )
    op.execute("ALTER TABLE review_operations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE review_operations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY review_operation_owner ON review_operations FOR ALL
      USING(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
      WITH CHECK(usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)""")
    op.execute(
        "CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON review_operations FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
    )
    # Same narrow account-erasure exception and immutable audit as Result operations.
    op.execute(
        "CREATE TRIGGER review_operation_immutable BEFORE UPDATE OR DELETE ON review_operations FOR EACH ROW EXECUTE FUNCTION result_operation_immutable()"
    )
    op.execute("""DO $$ DECLARE entry record; BEGIN
      FOR entry IN SELECT DISTINCT grantee::regrole::text AS role FROM aclexplode(
        (SELECT relacl FROM pg_class WHERE oid='public.apostas'::regclass))
        WHERE privilege_type='SELECT' AND grantee<>0 LOOP
        EXECUTE format('GRANT SELECT,INSERT,DELETE ON review_operations TO %I',entry.role);
      END LOOP; END $$""")


def downgrade() -> None:
    op.execute("SET LOCAL row_security=off")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM review_operations) THEN
      RAISE EXCEPTION 'preserve human review receipts before downgrade'; END IF; END $$""")
    op.drop_table("review_operations")
    op.drop_constraint("uq_review_receipt_owner", "revisao_pendente", type_="unique")
    for table in reversed(EVIDENCE_TABLES):
        op.execute(f"DROP TRIGGER review_evidence_revision ON {table}")
        op.drop_column(table, "review_revision")
    op.execute("DROP FUNCTION review_evidence_revision()")
