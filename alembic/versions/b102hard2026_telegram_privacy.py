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
    op.execute("""
        CREATE FUNCTION telegram_legacy_media_visible(target text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT NOT EXISTS (SELECT 1 FROM public.rascunhos_aposta WHERE media_hash=target)
          OR EXISTS (SELECT 1 FROM public.rascunhos_aposta WHERE media_hash=target
            AND usuario_id=NULLIF(current_setting('app.current_user_id',true),'')::bigint)
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION telegram_legacy_media_visible(text) FROM PUBLIC")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
      GRANT EXECUTE ON FUNCTION telegram_legacy_media_visible(text) TO bancaemdia_app;
    END IF; END $$""")
    for table in ("midias", "midia_arquivos"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY telegram_legacy_media ON {table} FOR ALL "
            "USING (telegram_legacy_media_visible(hash)) WITH CHECK (telegram_legacy_media_visible(hash))"
        )

    op.execute("""
        CREATE FUNCTION telegram_purge_link_metadata(cutoff timestamptz, batch integer)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN
          IF current_setting('app.telegram_transport',true) IS DISTINCT FROM 'on'
             OR cutoff > clock_timestamp()-interval '1 day' OR batch NOT BETWEEN 1 AND 1000 THEN
            RAISE EXCEPTION 'invalid retention context';
          END IF;
          DELETE FROM public.telegram_link_codes WHERE id IN (
            SELECT id FROM public.telegram_link_codes WHERE expires_at < cutoff
            ORDER BY expires_at LIMIT batch FOR UPDATE SKIP LOCKED);
          DELETE FROM public.telegram_links WHERE id IN (
            SELECT id FROM public.telegram_links WHERE revoked_at < cutoff
            ORDER BY revoked_at LIMIT batch FOR UPDATE SKIP LOCKED);
          DELETE FROM public.telegram_link_attempts WHERE identity_hash IN (
            SELECT identity_hash FROM public.telegram_link_attempts WHERE last_failed_at < cutoff
            ORDER BY last_failed_at LIMIT batch FOR UPDATE SKIP LOCKED);
          DELETE FROM public.telegram_link_attempt_events WHERE id IN (
            SELECT id FROM public.telegram_link_attempt_events WHERE occurred_at < cutoff
            ORDER BY occurred_at LIMIT batch FOR UPDATE SKIP LOCKED);
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION telegram_purge_link_metadata(timestamptz,integer) FROM PUBLIC"
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
      GRANT EXECUTE ON FUNCTION telegram_purge_link_metadata(timestamptz,integer) TO bancaemdia_app;
    END IF; END $$""")

    op.execute("""
        CREATE FUNCTION telegram_chat_linked(owner_id bigint, chat bigint) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
          SELECT EXISTS(SELECT 1 FROM public.telegram_links WHERE usuario_id=owner_id
                        AND telegram_chat_id=chat AND revoked_at IS NULL)
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION telegram_chat_linked(bigint,bigint) FROM PUBLIC")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_app') THEN
      GRANT EXECUTE ON FUNCTION telegram_chat_linked(bigint,bigint) TO bancaemdia_app;
    END IF; END $$""")

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
    for table in ("midias", "midia_arquivos"):
        op.execute(f"DROP POLICY telegram_legacy_media ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION telegram_chat_linked(bigint,bigint)")
    op.execute("DROP FUNCTION telegram_purge_link_metadata(timestamptz,integer)")
    op.execute("DROP FUNCTION telegram_legacy_media_visible(text)")
    op.execute("DROP FUNCTION telegram_purge_legacy_media(text,timestamptz)")
    op.drop_table("telegram_media")
    op.drop_constraint("uq_draft_origin_digest", "rascunhos_aposta")
    op.drop_column("rascunhos_aposta", "purged_at")
    op.drop_column("rascunhos_aposta", "origin_digest")
    op.drop_table("telegram_rate_buckets")
