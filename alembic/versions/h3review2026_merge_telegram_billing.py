"""Converge Telegram and billing; preserve terminal data erasure after expiry."""

from alembic import op

revision = "h3review2026"
down_revision = ("h2review2026", "b102hard2026")
branch_labels = None
depends_on = None

PRIVACY_SQL = "-- Apply in the administrative convergence migration AFTER both #154 and #102.\n-- Only removal of non-financial, terminal bot data may bypass subscription expiry.\nDO $privacy$\nDECLARE definition text;\nBEGIN\n  IF to_regprocedure('billing_require_write()') IS NOT NULL THEN\n    SELECT pg_get_functiondef('billing_require_write()'::regprocedure) INTO definition;\n    IF strpos(definition, 'telegram_retention_erasure') = 0 THEN\n      IF strpos(definition, 'uid :=') = 0 THEN\n        RAISE EXCEPTION 'unknown billing guard contract';\n      END IF;\n      definition := replace(definition, 'uid :=', $injection$\n        -- telegram_retention_erasure: no new financial value or active data.\n        IF TG_TABLE_NAME='rascunho_correcoes' AND TG_OP='DELETE' THEN RETURN OLD; END IF;\n        IF TG_TABLE_NAME='rascunhos_aposta' AND TG_OP='UPDATE' THEN\n        IF OLD.status IN ('CONFIRMED','CANCELLED','FAILED')\n          AND (to_jsonb(NEW)-ARRAY['origin_digest','purged_at','telegram_chat_id',\n            'telegram_message_id','media_reference_ciphertext','media_hash',\n            'source_metadata_json','fields_json','field_meta_json','missing_fields_json',\n            'coupon_candidates_json']) =\n              (to_jsonb(OLD)-ARRAY['origin_digest','purged_at','telegram_chat_id',\n            'telegram_message_id','media_reference_ciphertext','media_hash',\n            'source_metadata_json','fields_json','field_meta_json','missing_fields_json',\n            'coupon_candidates_json'])\n          AND (NEW.origin_digest IS NOT DISTINCT FROM OLD.origin_digest OR OLD.origin_digest IS NULL)\n          AND NEW.telegram_chat_id IN (0, OLD.telegram_chat_id)\n          AND NEW.telegram_message_id IN (OLD.telegram_message_id, OLD.telegram_update_id)\n          AND (NEW.media_reference_ciphertext IS NULL OR NEW.media_reference_ciphertext=OLD.media_reference_ciphertext)\n          AND (NEW.media_hash IS NULL OR NEW.media_hash=OLD.media_hash)\n          AND NEW.source_metadata_json IN ('{}'::jsonb, OLD.source_metadata_json)\n          AND NEW.fields_json IN ('{}'::jsonb, OLD.fields_json)\n          AND NEW.field_meta_json IN ('{}'::jsonb, OLD.field_meta_json)\n          AND NEW.missing_fields_json IN ('[]'::jsonb, OLD.missing_fields_json)\n          AND NEW.coupon_candidates_json IN ('[]'::jsonb, OLD.coupon_candidates_json)\n        THEN RETURN NEW; END IF;\n        END IF;\n        uid :=\n      $injection$);\n      EXECUTE definition;\n    END IF;\n  END IF;\nEND $privacy$;\n"


def upgrade() -> None:
    op.execute("SELECT billing_install_write_guards()")
    op.execute(PRIVACY_SQL)


def downgrade() -> None:
    # Retain erasure compatibility until the owning billing migration drops the guard.
    pass
