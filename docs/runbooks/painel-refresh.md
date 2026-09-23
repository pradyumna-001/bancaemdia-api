# Painel materialized-view refresh

The dashboard reads six materialized views from the read replica. Their source data is financial
and tenant-scoped, so the materializations live in the private `painel` schema. The HTTP database
role must receive access only to the `public.painel_*` security-barrier views; it must not receive
`USAGE` on `painel` or direct access to its materialized views.

When migrations and HTTP use separate roles, grant the HTTP role only the public surface after
the migration (replace `bancaemdia_app` with the deployment's role):

```sql
GRANT SELECT ON
  public.painel_resumo,
  public.painel_por_casa,
  public.painel_por_tipster,
  public.painel_por_mercado,
  public.painel_por_periodo,
  public.painel_evolucao_banca,
  public.painel_atualizacao
TO bancaemdia_app;
```

Do not grant that role `USAGE` on schema `painel`. The integration suite verifies both the public
tenant filter and denial of direct materialized-view access.

## Schedule

Migration 008 registers the named job `bancaemdia_painel_refresh` every 15 seconds when
`pg_cron` is installed and usable by the migration owner; migration 010 changes an existing
five-minute job to 15 seconds. Terraform configures RDS PostgreSQL 16 to preload `pg_cron`
in the `bancaemdia` database. After the parameter group becomes active, run this once with
the primary migration/maintenance credential to install the extension and create or update the job:

```bash
python -m bancaemdia.cli.configure_painel_cron
```

For a local database without `pg_cron`, schedule
`python -m bancaemdia.cli.refresh_painel` every 15 seconds using an external scheduler.

Do not run it with the read-only HTTP role or against the replica. The command needs ownership of
the materialized views. It takes advisory lock `20260930`, opens one repeatable-read transaction,
removes the OLTP statement timeout locally, refreshes all six views with `CONCURRENTLY`, and updates
`painel.estado_refresh.atualizado_em` only after every refresh succeeds. A competing invocation
exits instead of queuing another expensive rebuild. PostgreSQL also permits only one concurrent
refresh of a given materialized view at a time.

If a refresh fails, the transaction keeps the prior complete set and prior timestamp visible.
Investigate the database error and rerun the command; do not update `estado_refresh` by hand.

## Freshness contract

The public API deliberately reports three different values:

- `atualizado_em`: the persisted completion time of the last full six-view refresh;
- `idade_mv_segundos`: response time minus that persisted timestamp;
- `replica_atraso_segundos`: WAL replay lag when PostgreSQL can measure a standby, otherwise
  `null` on the primary, local single-node deployments, or unavailable replica telemetry.

`respondido_em` is only the time the response was built. It is never presented as data freshness.
`fresh=true` routes the main dashboard request to the primary, which removes replica replay lag but
does not force or pretend to force a materialized-view refresh.

The 15-second cadence leaves at most 15 seconds for refresh duration and replica lag to satisfy
the issue's “less than 30 seconds stale” criterion. Full refresh work may exceed that budget as
data grows. Measure duration, WAL volume, replica lag, and HTTP P95 with representative data;
if the budget fails, switch to incremental aggregation before claiming that target is met.

## Checks

As the maintenance owner on the primary:

```sql
SELECT atualizado_em,
       clock_timestamp() - atualizado_em AS idade
  FROM painel.estado_refresh
 WHERE id = 1;

SELECT schemaname, matviewname, ispopulated
  FROM pg_matviews
 WHERE schemaname = 'painel'
 ORDER BY matviewname;
```

When `pg_cron` is installed, check its own schema's `job` and `job_run_details` for the named job.
Alert on failed runs and on an age greater than two schedule intervals. A successful HTTP response
with an old `atualizado_em` is a stale-but-honest response, not evidence that refresh is healthy.

## Performance verification

`scripts/benchmark_painel.py` measures warmed HTTP P50/P95/P99 without printing the bearer token.
Populate a database with representative users, bets and dimension cardinality, record hardware and
dataset counts, then set `PAINEL_BENCH_TOKEN` and `PAINEL_BENCH_URL`. Do not claim P95 below 500 ms
from an empty database or from unit-test timings.
