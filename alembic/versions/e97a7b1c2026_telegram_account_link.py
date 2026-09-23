"""Single-use Telegram account links with tenant isolation.

Revision ID: e97a7b1c2026
Revises: d94a7b1c2026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e97a7b1c2026"
down_revision: str | Sequence[str] | None = "d94a7b1c2026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
TENANT_TABLES = ("telegram_links", "telegram_link_codes")


def upgrade() -> None:
    op.create_table(
        "telegram_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("usuario_id", "telegram_user_id", name="uq_telegram_link_pair"),
    )
    op.create_index(
        "uq_telegram_link_active_user",
        "telegram_links",
        ["usuario_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_telegram_link_active_identity",
        "telegram_links",
        ["telegram_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "telegram_link_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.BigInteger(), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "failed_attempts BETWEEN 0 AND 5", name="ck_telegram_link_code_attempts"
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_telegram_link_code_expiry"),
        sa.UniqueConstraint("code_hash", name="uq_telegram_link_code_hash"),
    )
    op.create_index(
        "idx_telegram_link_codes_user_issued", "telegram_link_codes", ["usuario_id", "issued_at"]
    )
    op.create_table(
        "telegram_link_attempts",
        sa.Column("identity_hash", sa.String(64), primary_key=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "failed_attempts BETWEEN 0 AND 5", name="ck_telegram_link_attempt_count"
        ),
    )
    op.create_table(
        "telegram_link_attempt_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('INVALID','BLOCKED')", name="ck_telegram_link_attempt_outcome"
        ),
    )
    for table in (*TENANT_TABLES, "telegram_link_attempts", "telegram_link_attempt_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Function owner must bypass RLS for an exact-hash lookup before the
        # caller's tenant is known. The application role remains subject to RLS.
    for table in TENANT_TABLES:
        op.execute(
            f"CREATE POLICY {table}_por_usuario ON {table} FOR ALL USING (usuario_id = {TENANT}) WITH CHECK (usuario_id = {TENANT})"
        )
        op.execute(
            f"CREATE TRIGGER audit_{table}_write AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION audit_tenant_write()"
        )
        op.execute(
            f"CREATE TRIGGER active_{table}_write BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION require_active_tenant()"
        )
    # These narrow functions bridge the unknown issuer/identity before a tenant is known.
    # They return only an owner ID for an exact keyed hash or active Telegram identity.
    op.execute("""
        CREATE FUNCTION telegram_link_code_issuer(p_hash text) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT usuario_id FROM public.telegram_link_codes WHERE code_hash = p_hash
        $$
    """)
    op.execute("""
        CREATE FUNCTION telegram_link_active_owner(p_user_id bigint, p_chat_id bigint) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT usuario_id FROM public.telegram_links
           WHERE telegram_user_id = p_user_id
             AND revoked_at IS NULL
        $$
    """)
    op.execute("""
        CREATE FUNCTION telegram_link_attempt_state(p_hash text) RETURNS timestamptz
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT blocked_until FROM public.telegram_link_attempts WHERE identity_hash = p_hash
        $$
    """)
    op.execute("""
        CREATE FUNCTION telegram_link_record_failure(p_hash text, p_outcome text) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE moment timestamptz := clock_timestamp();
        BEGIN
          IF p_outcome NOT IN ('INVALID', 'BLOCKED') OR p_hash !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'invalid attempt data';
          END IF;
          INSERT INTO public.telegram_link_attempts
            (identity_hash, failed_attempts, window_started_at, blocked_until, last_failed_at)
          VALUES (p_hash, 1, moment, NULL, moment)
          ON CONFLICT (identity_hash) DO UPDATE SET
            failed_attempts = CASE
              WHEN EXCLUDED.last_failed_at - telegram_link_attempts.window_started_at >= interval '30 minutes' THEN 1
              ELSE LEAST(5, telegram_link_attempts.failed_attempts + 1) END,
            window_started_at = CASE
              WHEN EXCLUDED.last_failed_at - telegram_link_attempts.window_started_at >= interval '30 minutes' THEN EXCLUDED.last_failed_at
              ELSE telegram_link_attempts.window_started_at END,
            blocked_until = CASE
              WHEN EXCLUDED.last_failed_at - telegram_link_attempts.window_started_at >= interval '30 minutes' THEN NULL
              WHEN telegram_link_attempts.failed_attempts >= 4 THEN EXCLUDED.last_failed_at + interval '30 minutes'
              ELSE telegram_link_attempts.blocked_until END,
            last_failed_at = EXCLUDED.last_failed_at;
          INSERT INTO public.telegram_link_attempt_events (outcome) VALUES (p_outcome);
        END $$
    """)
    op.execute("""
        CREATE FUNCTION telegram_link_clear_attempts(p_hash text) RETURNS void
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          DELETE FROM public.telegram_link_attempts WHERE identity_hash = p_hash
        $$
    """)
    # Only the application DB role should execute the bridges. Deployment roles with
    # a different name need equivalent EXECUTE grants after migration.
    for signature in (
        "telegram_link_code_issuer(text)",
        "telegram_link_active_owner(bigint,bigint)",
        "telegram_link_attempt_state(text)",
        "telegram_link_record_failure(text,text)",
        "telegram_link_clear_attempts(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bancaemdia_app') THEN
            GRANT EXECUTE ON FUNCTION telegram_link_code_issuer(text),
              telegram_link_active_owner(bigint,bigint), telegram_link_attempt_state(text),
              telegram_link_record_failure(text,text), telegram_link_clear_attempts(text)
              TO bancaemdia_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    for signature in (
        "telegram_link_clear_attempts(text)",
        "telegram_link_record_failure(text,text)",
        "telegram_link_attempt_state(text)",
        "telegram_link_active_owner(bigint,bigint)",
        "telegram_link_code_issuer(text)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    for table in TENANT_TABLES:
        op.execute(f"DROP TRIGGER active_{table}_write ON {table}")
        op.execute(f"DROP TRIGGER audit_{table}_write ON {table}")
        op.execute(f"DROP POLICY {table}_por_usuario ON {table}")
    op.drop_table("telegram_link_attempt_events")
    op.drop_table("telegram_link_attempts")
    op.drop_index("idx_telegram_link_codes_user_issued", table_name="telegram_link_codes")
    op.drop_table("telegram_link_codes")
    op.drop_index("uq_telegram_link_active_identity", table_name="telegram_links")
    op.drop_index("uq_telegram_link_active_user", table_name="telegram_links")
    op.drop_table("telegram_links")
