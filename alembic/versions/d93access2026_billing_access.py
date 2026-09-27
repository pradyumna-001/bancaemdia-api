"""Issue #93: billing_access."""

from alembic import op

revision = "d93access2026"
down_revision = "d92events2026"
branch_labels = None
depends_on = None

WRITE_TABLES = (
    "bancas",
    "contas_casa",
    "unidades",
    "movimentos",
    "movimento_requisicoes",
    "apostas",
    "eventos",
    "revisao_pendente",
    "coletas_casa",
    "coleta_token",
    "chamadas_ia",
    "uploads",
    "upload_bilhetes",
    "upload_arquivos",
)


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION billing_require_write() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE uid bigint; t timestamptz; allowed boolean; grant_row record;
        BEGIN
            uid := COALESCE(to_jsonb(NEW)->>'usuario_id', to_jsonb(OLD)->>'usuario_id')::bigint;
            IF uid IS NULL OR NOT EXISTS (SELECT 1 FROM public.billing_rollout WHERE activated_at IS NOT NULL) THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
            END IF;
            SELECT * INTO grant_row FROM public.assinaturas WHERE usuario_id=uid FOR SHARE;
            t := clock_timestamp();
            allowed := (grant_row.trial_confirmed AND grant_row.trial_started_at <= t AND t < grant_row.trial_ends_at)
                OR (grant_row.status='ACTIVE' AND grant_row.current_period_started_at <= t AND t < grant_row.current_period_ends_at);
            IF allowed IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'account_read_only' USING ERRCODE='P0402';
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
        END $billing$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_require_write() FROM PUBLIC")
    for table in WRITE_TABLES:
        op.execute(
            f"CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION billing_require_write()"
        )


def downgrade() -> None:
    for table in WRITE_TABLES:
        op.execute(f"DROP TRIGGER billing_write_guard ON {table}")
    op.execute("DROP FUNCTION billing_require_write()")
