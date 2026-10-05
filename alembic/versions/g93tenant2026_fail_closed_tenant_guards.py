"""Validate billing guard tenant columns and reject rows without an owner."""

from alembic import op

revision = "g93tenant2026"
down_revision = "f154access2026"
branch_labels = None
depends_on = None

# Explicit mapping also covers optional modules installed before or after billing.
TENANT_COLUMNS = dict.fromkeys(
    (
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
        "metas_desempenho",
    ),
    "usuario_id",
)


def upgrade() -> None:
    # Keep the published function signature: reciprocal module migrations call it too.
    op.execute("""
        DO $fix$ DECLARE definition text;
        BEGIN
            SELECT pg_get_functiondef('public.billing_require_write()'::regprocedure)
                INTO definition;
            definition := replace(definition,
                'IF uid IS NULL OR NOT EXISTS',
                'IF uid IS NULL THEN RAISE EXCEPTION ''billing tenant missing'' USING ERRCODE=''P0402''; END IF; IF NOT EXISTS');
            definition := replace(definition, '-- Cancellation and deferred-queue metadata', $erasure$
            -- Authenticated account erasure cannot create or edit business data.
            IF uid = NULLIF(current_setting('app.erase_user_data', true), '')::bigint
               AND uid = NULLIF(current_setting('app.current_user_id', true), '')::bigint THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                IF TG_TABLE_NAME='eventos' AND TG_OP='UPDATE' THEN
                  IF NEW.payload_json='{}'::jsonb AND NEW.chat_id IS NULL
                   AND NEW.message_id IS NULL AND NEW.aposta_chave IS NULL
                   AND NEW.confianca IS NULL
                   AND (to_jsonb(NEW)-ARRAY['payload_json','chat_id','message_id','aposta_chave','confianca'])
                       = (to_jsonb(OLD)-ARRAY['payload_json','chat_id','message_id','aposta_chave','confianca']) THEN
                      RETURN NEW;
                  END IF;
                END IF;
            END IF;
            -- Cancellation and deferred-queue metadata
            $erasure$);
            EXECUTE definition;
        END $fix$
    """)
    mapping = ",".join(f"('{table}','{column}')" for table, column in TENANT_COLUMNS.items())
    op.execute(f"""
        CREATE OR REPLACE FUNCTION billing_install_write_guards() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $billing$
        DECLARE item record; target regclass;
        BEGIN
            FOR item IN SELECT * FROM (VALUES {mapping}) AS mapping(table_name, tenant_column)
            LOOP
                target := to_regclass('public.' || item.table_name);
                IF target IS NULL THEN CONTINUE; END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_attribute WHERE attrelid=target
                    AND attname=item.tenant_column AND NOT attisdropped
                    AND atttypid='bigint'::regtype
                ) THEN
                    RAISE EXCEPTION 'billing tenant column invalid: %.%', item.table_name,
                        item.tenant_column USING ERRCODE='P0402';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgrelid=target
                        AND tgname='billing_write_guard' AND NOT tgisinternal
                ) THEN
                    EXECUTE format('CREATE TRIGGER billing_write_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.billing_require_write()', item.table_name);
                END IF;
            END LOOP;
        END $billing$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing_install_write_guards() FROM PUBLIC")
    op.execute("SELECT billing_install_write_guards()")


def downgrade() -> None:
    # Retain the stricter functions until the owning d93 migration drops them.
    # Restoring the missing-owner bypass is not a safe rollback.
    pass
