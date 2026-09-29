"""Shared transactional quotas and private bot media."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "b102hard2026"
down_revision = "f141chain2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_rate_buckets",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("ALTER TABLE telegram_rate_buckets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE telegram_rate_buckets FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY telegram_rate_transport ON telegram_rate_buckets FOR ALL "
        "USING (current_setting('app.telegram_transport', true) = 'on') "
        "WITH CHECK (current_setting('app.telegram_transport', true) = 'on')"
    )
    op.add_column("rascunhos_aposta", sa.Column("origin_digest", sa.String(64)))
    op.add_column("rascunhos_aposta", sa.Column("purged_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        "uq_draft_origin_digest", "rascunhos_aposta", ["usuario_id", "origin_digest"]
    )
    op.create_table(
        "telegram_media",
        sa.Column("draft_id", sa.UUID(), primary_key=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("content_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id", "draft_id"],
            ["rascunhos_aposta.usuario_id", "rascunhos_aposta.id"],
            ondelete="CASCADE",
        ),
    )
    op.execute("ALTER TABLE telegram_media ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE telegram_media FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY telegram_media_owner ON telegram_media FOR ALL "
        "USING (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint) "
        "WITH CHECK (usuario_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)"
    )
    # Legacy bot blobs can share hashes with imports: remove only unowned blobs,
    # after every bot draft using the hash is closed and past its retention.
    op.execute("""
        CREATE FUNCTION telegram_purge_legacy_media(target text, cutoff timestamptz)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
          IF current_setting('app.telegram_transport', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION 'transport context required';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM rascunhos_aposta WHERE media_hash = target
              AND (closed_at IS NULL OR closed_at >= cutoff))
            AND NOT EXISTS (SELECT 1 FROM mensagens WHERE midia_hash = target)
            AND NOT EXISTS (SELECT 1 FROM apostas WHERE midia_hash = target AND origem <> 'telegram_bot')
            AND NOT EXISTS (SELECT 1 FROM revisao_pendente WHERE midia_hash = target) THEN
            DELETE FROM midia_arquivos WHERE hash = target AND s3_key IS NULL;
            DELETE FROM midias WHERE hash = target AND NOT EXISTS
                (SELECT 1 FROM midia_arquivos WHERE hash = target);
          END IF;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION telegram_purge_legacy_media(text,timestamptz) FROM PUBLIC")
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
          GRANT EXECUTE ON FUNCTION telegram_purge_legacy_media(text,timestamptz) TO bancaemdia_app;
        END IF; END $$;
    """)

    op.execute(
        (Path(__file__).resolve().parents[2] / "scripts/telegram_billing_privacy.sql").read_text(
            encoding="utf-8"
        )
    )


def downgrade() -> None:
    op.execute("""
        DO $$ DECLARE definition text; BEGIN
          IF to_regprocedure('billing_require_write()') IS NOT NULL THEN
            SELECT pg_get_functiondef('billing_require_write()'::regprocedure) INTO definition;
            definition := regexp_replace(definition, '-- telegram_retention_erasure:.*?uid :=', 'uid :=', 's');
            EXECUTE definition;
          END IF;
        END $$
    """)
    # Never silently discard retained private media during rollback.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM telegram_media) THEN "
        "RAISE EXCEPTION 'purge Telegram media before downgrade'; END IF; END $$"
    )
    op.execute("DROP FUNCTION telegram_purge_legacy_media(text,timestamptz)")
    op.drop_table("telegram_media")
    op.drop_constraint("uq_draft_origin_digest", "rascunhos_aposta")
    op.drop_column("rascunhos_aposta", "purged_at")
    op.drop_column("rascunhos_aposta", "origin_digest")
    op.drop_table("telegram_rate_buckets")
