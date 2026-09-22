# Apply a database schema migration

**Scope:** PostgreSQL 16 through Alembic. Use a migration-capable role on the primary;
the API's tenant role and read replica are not suitable. Run data backfills separately
from schema expansion. Preserve the append-only event and cash ledgers.

## Preflight and rehearsal

1. List revisions with `alembic history` and `alembic current`. Review each `upgrade()` and
   `downgrade()` in the release, including locks, index creation, RLS grants, and compatibility
   with the old application. Require expand-only changes while both versions may run.
2. Rehearse the exact release against a restored staging copy. Measure migration duration,
   lock waits, index validity, application compatibility, and
   `python scripts/conferir_numeros.py --todos`. Stop if any check fails.
3. Record the RDS instance ID and make a pre-change snapshot; wait until it is available.
   For an RDS DB **instance** (not a DB cluster), with operator-supplied identifiers:

   ```bash
   aws rds create-db-snapshot --db-instance-identifier "$RDS_INSTANCE" --db-snapshot-identifier "$SNAPSHOT_ID"
   aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAPSHOT_ID"
   ```

4. Save the schema separately using a configured libpq service and protected password file:

   ```bash
   PGSERVICE=bancaemdia-production pg_dump --schema-only --no-owner --no-privileges \
     --file="schema-before-$SHA.sql"
   ```

   Restrict and retain that file under the backup policy.
   `DATABASE_URL` for the Python app uses `postgresql+asyncpg://`; do not pass it directly to
   `pg_dump`, which expects a libpq connection. Verify backup access before proceeding.

## Apply and verify

1. Announce the change window and pause conflicting jobs if the rehearsal showed lock risk.
   With `DATABASE_URL` set by the approved secret manager to the **primary** migration role:
   `alembic upgrade head`. Do not run a data backfill in the same change step.
2. Check `alembic current` against the expected revision, then run
   `python scripts/conferir_numeros.py --todos`. Stop rollout on any divergence.
3. Check invalid indexes (`SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;`),
   long-running queries and lock waits, and the application's `/ready` and smoke tests. Run
   `ANALYZE` on the specific large tables changed by the migration after measuring its cost;
   review the affected query plans, rather than assuming a new index is used immediately.
4. Record revision, snapshot ID, schema dump location, duration, integrity result, and any
   follow-up backfill plan.

## If the migration fails

Leave the previous app version running if the expanded schema remains compatible. A
`alembic downgrade -1` is permitted **only** when that specific revision's downgrade was
reviewed and rehearsed, no post-upgrade writes depend on it, and the old app remains compatible.
Otherwise preserve evidence and plan a forward fix. Database restore or point-in-time recovery
is a separate incident decision because it can lose newer writes. See the
[RDS snapshot CLI](https://docs.aws.amazon.com/cli/latest/reference/rds/create-db-snapshot.html).
