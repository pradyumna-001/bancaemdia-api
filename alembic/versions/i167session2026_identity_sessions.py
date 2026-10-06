"""Private identity links, one-time OIDC flows and revocable server sessions."""

from alembic import op

revision = "i167session2026"
down_revision = "a9d6e3f1c210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bancaemdia_auth') THEN
        CREATE ROLE bancaemdia_auth NOLOGIN NOBYPASSRLS;
      END IF;
    END $$;
    """)
    schema = """
    CREATE SCHEMA auth_private;
    REVOKE ALL ON SCHEMA auth_private FROM PUBLIC;
    CREATE TABLE auth_private.identities (
      id uuid PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
      usuario_id bigint NOT NULL UNIQUE REFERENCES public.usuarios(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(issuer,subject), CHECK(length(subject) BETWEEN 1 AND 255)
    );
    CREATE TABLE auth_private.flows (
      state_hash text PRIMARY KEY, browser_hash text NOT NULL,
      encrypted text NOT NULL, expires_at timestamptz NOT NULL,
      consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE auth_private.sessions (
      id uuid PRIMARY KEY, identity_id uuid NOT NULL REFERENCES auth_private.identities(id),
      cookie_hash text NOT NULL UNIQUE, encrypted text NOT NULL,
      generation integer NOT NULL DEFAULT 0 CHECK(generation>=0),
      expires_at timestamptz NOT NULL, idle_until timestamptz NOT NULL,
      access_until timestamptz NOT NULL, revoked_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX auth_sessions_identity ON auth_private.sessions(identity_id);
    CREATE TABLE auth_private.retired_cookies (
      cookie_hash text PRIMARY KEY, session_id uuid NOT NULL REFERENCES auth_private.sessions(id)
    );
    CREATE TABLE auth_private.revocations (
      session_id uuid PRIMARY KEY REFERENCES auth_private.sessions(id), encrypted text NOT NULL,
      attempts integer NOT NULL DEFAULT 0, retry_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE auth_private.audit (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      usuario_id bigint, session_id uuid, action text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK(action IN ('provision','login','refresh','logout','reuse','expiry'))
    );
    REVOKE ALL ON ALL TABLES IN SCHEMA auth_private FROM PUBLIC;
    GRANT USAGE ON SCHEMA auth_private TO bancaemdia_auth;
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA auth_private TO bancaemdia_auth;
    REVOKE UPDATE,DELETE ON auth_private.identities,auth_private.audit FROM bancaemdia_auth;
    GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA auth_private TO bancaemdia_auth;
    GRANT SELECT,INSERT ON public.usuarios TO bancaemdia_auth;
    GRANT USAGE,SELECT ON SEQUENCE public.usuarios_id_seq TO bancaemdia_auth;
    CREATE POLICY usuarios_identidade_leitura ON public.usuarios
      FOR SELECT TO bancaemdia_auth USING(true);
    CREATE POLICY usuarios_identidade_cadastro ON public.usuarios
      FOR INSERT TO bancaemdia_auth WITH CHECK(true);
    """
    for statement in schema.split(";"):
        if statement.strip():
            op.execute(statement.strip())


def downgrade() -> None:
    # Sessions cannot survive an application that ignores their revocation state.
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM auth_private.identities) THEN
        RAISE EXCEPTION 'Identity links exist; preserve schema and revocation ledger on rollback';
      END IF;
    END $$;
    """)
    op.execute("DROP POLICY usuarios_identidade_leitura ON public.usuarios")
    op.execute("DROP POLICY usuarios_identidade_cadastro ON public.usuarios")
    op.execute("DROP SCHEMA auth_private CASCADE")
