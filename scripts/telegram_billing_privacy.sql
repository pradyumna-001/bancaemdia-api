-- Apply in the administrative convergence migration AFTER both #154 and #102.
-- Only removal of non-financial, terminal bot data may bypass subscription expiry.
DO $privacy$
DECLARE definition text;
BEGIN
  IF to_regprocedure('billing_require_write()') IS NOT NULL THEN
    SELECT pg_get_functiondef('billing_require_write()'::regprocedure) INTO definition;
    IF strpos(definition, 'telegram_retention_erasure') = 0 THEN
      IF strpos(definition, 'uid :=') = 0 THEN
        RAISE EXCEPTION 'unknown billing guard contract';
      END IF;
      definition := replace(definition, 'uid :=', $injection$
        -- telegram_retention_erasure: no new financial value or active data.
        IF TG_TABLE_NAME='rascunho_correcoes' AND TG_OP='DELETE' THEN RETURN OLD; END IF;
        IF TG_TABLE_NAME='rascunhos_aposta' AND TG_OP='UPDATE' THEN
        IF OLD.status IN ('CONFIRMED','CANCELLED','FAILED')
          AND (to_jsonb(NEW)-ARRAY['origin_digest','purged_at','telegram_chat_id',
            'telegram_message_id','media_reference_ciphertext','media_hash',
            'source_metadata_json','fields_json','field_meta_json','missing_fields_json',
            'coupon_candidates_json']) =
              (to_jsonb(OLD)-ARRAY['origin_digest','purged_at','telegram_chat_id',
            'telegram_message_id','media_reference_ciphertext','media_hash',
            'source_metadata_json','fields_json','field_meta_json','missing_fields_json',
            'coupon_candidates_json'])
          AND (NEW.origin_digest IS NOT DISTINCT FROM OLD.origin_digest OR OLD.origin_digest IS NULL)
          AND NEW.telegram_chat_id IN (0, OLD.telegram_chat_id)
          AND NEW.telegram_message_id IN (OLD.telegram_message_id, OLD.telegram_update_id)
          AND (NEW.media_reference_ciphertext IS NULL OR NEW.media_reference_ciphertext=OLD.media_reference_ciphertext)
          AND (NEW.media_hash IS NULL OR NEW.media_hash=OLD.media_hash)
          AND NEW.source_metadata_json IN ('{}'::jsonb, OLD.source_metadata_json)
          AND NEW.fields_json IN ('{}'::jsonb, OLD.fields_json)
          AND NEW.field_meta_json IN ('{}'::jsonb, OLD.field_meta_json)
          AND NEW.missing_fields_json IN ('[]'::jsonb, OLD.missing_fields_json)
          AND NEW.coupon_candidates_json IN ('[]'::jsonb, OLD.coupon_candidates_json)
        THEN RETURN NEW; END IF;
        END IF;
        uid :=
      $injection$);
      EXECUTE definition;
    END IF;
  END IF;
END $privacy$;
