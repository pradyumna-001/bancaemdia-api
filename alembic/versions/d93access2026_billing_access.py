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
    "titulares",
    "usos_conta_casa",
    "trocas_titular_requisicoes",
    "trocas_titular_eventos",
    "rascunhos_aposta",
    "rascunho_correcoes",
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
            -- Cancellation and deferred-queue metadata are control operations.
            -- No business fields, tenant or media may change through these exceptions.
            IF TG_TABLE_NAME='rascunhos_aposta' AND TG_OP='UPDATE' THEN
                IF NEW.status='CANCELLED' AND OLD.status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION','AWAITING_CONFIRMATION')
                   AND NEW.version=OLD.version+1 AND NEW.closed_at IS NOT NULL
                   AND (to_jsonb(NEW)-ARRAY['status','version','closed_at']) = (to_jsonb(OLD)-ARRAY['status','version','closed_at']) THEN
                    RETURN NEW;
                END IF;
                IF NEW.extraction_error_code='account_read_only'
                   AND NEW.extraction_lease_token IS NULL AND NEW.extraction_lease_until IS NULL
                   AND (to_jsonb(NEW)-ARRAY['extraction_error_code','extraction_next_attempt_at','extraction_lease_token','extraction_lease_until'])
                       = (to_jsonb(OLD)-ARRAY['extraction_error_code','extraction_next_attempt_at','extraction_lease_token','extraction_lease_until']) THEN
                    RETURN NEW;
                END IF;
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
    tables_sql = ",".join(f"'{table}'" for table in WRITE_TABLES)
    op.execute(f"""
        CREATE FUNCTION billing_install_write_guards() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE table_name text;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[{tables_sql}] LOOP
                IF to_regclass('public.' || table_name) IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass('public.' || table_name)
                        AND tgname='billing_write_guard' AND NOT tgisinternal
                ) THEN
                    EXECUTE format('CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.billing_require_write()', table_name);
                END IF;
            END LOOP;
        END $billing$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_install_write_guards() FROM PUBLIC")
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    for table in WRITE_TABLES:
        op.execute(
            f"DO $$ BEGIN IF to_regclass('public.{table}') IS NOT NULL THEN "
            f"DROP TRIGGER IF EXISTS billing_write_guard ON {table}; END IF; END $$"
        )
    op.execute("DROP FUNCTION billing_install_write_guards()")
    op.execute("DROP FUNCTION billing_require_write()")
