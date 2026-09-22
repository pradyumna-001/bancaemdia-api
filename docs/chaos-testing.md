# Failure injection

Run the deterministic suite with `python scripts/chaos_inject.py local`. It uses the same
Testcontainers PostgreSQL fixture as the integration tests. Without Docker or
`TEST_DATABASE_URL`, the database cases are skipped; review the pytest skip count before
claiming coverage. `--verify-all` also runs `conferir_numeros.py --todos` against the
configured `DATABASE_URL` after the suite.

| Fault | Automated evidence |
| --- | --- |
| Worker process killed inside a materialization transaction | Child process is killed after its first event insert; PostgreSQL rolls back both event and projection. Redelivery creates one bet and `reconstruir_usuario` finds no divergence. |
| PostgreSQL primary unavailable | Readiness changes from 503 to 200 on recovery; a database write error is returned as 503. Materialization retries transient database errors. |
| Anthropic 5xx and timeout | Circuit breaker tests open after five 5xx errors. Timeout is excluded from breaker accounting but the Celery extraction task retries it. Open-breaker and exhausted-retry routing are tested separately. |
| Redis unavailable | Extraction cache becomes a miss. Paid Anthropic calls stop until the shared quota returns; Celery retries the task. API request limiting uses the existing process-local fallback. The Redis broker naturally stops delivery until it reconnects. |
| Client loses the response after commit | The same cash movement request is retried with one idempotency key. One movement and one request record remain. |
| PostgreSQL storage full | An injected write error rolls back the complete bet and event transaction. `materialization_failed_total{reason="disk_full"}` drives a critical alert. |

The tests verify logical recovery and integrity. They do not measure a real RDS failover or
network outage duration. For a staging Multi-AZ RDS instance, first inspect the command:

```text
python scripts/chaos_inject.py staging-failover --rds-instance STAGING_ID --aws-profile STAGING_PROFILE --region REGION --ready-url https://STAGING_API/ready
```

Set `APP_ENV=staging` and `DATABASE_URL` to the staging database. To execute, add
`--execute --confirm-instance STAGING_ID`. The script requires a healthy readiness
endpoint, an available Multi-AZ instance tagged `Environment=staging`, and an integrity
pass before and after failover. It polls readiness for up to 15 minutes and fails if a
healthy recovery is not observed. Run only in a staging maintenance window; AWS uses
`rds reboot-db-instance --force-failover` for a Multi-AZ DB instance. A Multi-AZ DB
cluster needs the separate `failover-db-cluster` operation and is not accepted here.
