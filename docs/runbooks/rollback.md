# Roll back an application release

**Target:** begin recovery immediately and restore a healthy service within five minutes of
the rollback decision. Treat this as a target to measure in staging, not a proven guarantee.
The CD workflow described here is pending [issue #42](https://github.com/pradyumna-001/bancaemdia-api/issues/42);
check the [current deployment boundary](../RUNBOOKS.md#current-deployment-boundary).

## Decide and prepare

1. Declare the incident and assign an operator. Use the deploy record to select the exact
   `PREVIOUS_SHA` and image digest last verified healthy in production. Confirm the image still
   exists and that the workflow can deploy that immutable artifact.
2. Check the schema changes since that release. An application rollback is safe only if the
   old code reads and writes the current schema. Do **not** automatically downgrade Alembic or
   restore a database snapshot: either can discard writes made since the deployment. If data
   integrity is in doubt, stop writes and follow [incident.md](incident.md) and
   [migration.md](migration.md).
3. Record the decision time, current SHA, prior SHA, alarm window, and rollback owner. Pause
   further promotions.

## Restore the previous application version

With `PREVIOUS_SHA` set to the known-good commit, dispatch the production rollback input
specified for `cd.yml`. This must reuse the previous image, not rebuild the old source.

```bash
gh workflow run cd.yml --ref main -f sha="$PREVIOUS_SHA" -f environment=production
gh run list --workflow cd.yml --limit 5
gh run watch "$ROLLBACK_RUN_ID"
```

Use the run ID for this dispatch and record its URL. The workflow must roll back API,
extraction, and materialization workers as one compatible release. If it is unavailable,
escalate to the incident owner; do not guess task-definition revisions or edit production
services by hand without an approved, recorded fallback plan.

## Verify and communicate

- Confirm ECS reports the previous image digest on every affected service and the expected
  task count is healthy. Check `/health` and `/ready` from the approved monitoring network;
  inspect every required readiness check, then run the synthetic authenticated smoke test.
- Watch 5xx rate, P99, queue depth, DLQ, replica lag, and breaker state for a full five-minute
  window. A 200 response alone is insufficient; queued tasks can still be failing.
- Run `python scripts/conferir_numeros.py --todos` with the authorized primary DB role. A
  nonzero result means the rollback is not closed and needs data-integrity investigation.
- Notify the on-call channel and affected stakeholders of impact, recovery time, release
  version, integrity result, and next update. Record whether the five-minute target was met.

Do not redeploy the failed SHA until the cause and a new validation plan are reviewed.
