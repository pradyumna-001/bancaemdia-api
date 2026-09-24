# Deploy a release

This ECS/RDS procedure is for Phase 2+. Use [the Lightsail Phase 1 runbook](lightsail-phase1.md)
for the administrator-approved launch architecture.

**Owner:** release operator with the on-call engineer present. **Gate:** this procedure becomes
executable after the CD workflow and AWS environments described in [the index](../RUNBOOKS.md)
are configured and rehearsed. The workflow is documented in [cd.md](cd.md).

## Before dispatch

1. Pin `SHA` to the reviewed commit and `PREVIOUS_SHA` to the currently healthy release; record
   both and the expected container image digest in the change ticket. Confirm all CI checks for
   `SHA` are green and the image exists. Do not deploy a moving branch name.
2. Review every Alembic revision between the two releases. Rehearse and verify it in staging
   using [migration.md](migration.md); make a snapshot before any production schema change.
   The old and new application versions must both work with the expanded schema.
3. Confirm staging smoke tests, queue health, replica lag, and
   `python scripts/conferir_numeros.py --todos` pass. Confirm an on-call owner, a previous
   release, and an available rollback path. Freeze unrelated deploys during the change.
4. Check `gh workflow view cd.yml` and its `workflow_dispatch` inputs on the default branch.
   Stop if the workflow or either target environment is absent; CI success alone is not a
   production deploy.

## Dispatch and observe

Use a shell with `SHA` set to the pinned commit. The workflow must require a manual production
approval and promote the exact image already tested in staging.

```bash
gh workflow run cd.yml --ref main -f sha="$SHA" -f environment=staging
gh run list --workflow cd.yml --limit 5
gh run watch "$STAGING_RUN_ID"
# After staging verification and the production approval gate:
gh workflow run cd.yml --ref main -f sha="$SHA" -f environment=production
gh run watch "$PRODUCTION_RUN_ID"
```

Read the run IDs returned by `gh run list`; do not assume the newest run belongs to your
dispatch. Record workflow links and deployment timestamps.

## Verify and decide

- From the approved monitoring network, `GET /health` and `GET /ready` must return 200.
  Inspect the readiness JSON, especially `postgres_primary`, `postgres_replica`, and `redis`.
  Anthropic and `celery_queue_depth` are report-only, so a 200 does not clear those faults.
- Run the workflow's authenticated synthetic-user smoke test for `/api/v1/painel` and the
  supported write path. Confirm the write is idempotent and inspect its result; keep tokens
  in the runner's secret store. There is no `/auth/login` route in this API.
- Compare the 5-minute 5xx rate and P99 latency to the pre-deploy baseline, plus Celery queue
  depths, DLQ, replica lag, breaker state, and materialization failures. Continue watching
  through the canary and full rollout, not just the first 200 response.
- Trigger [rollback](rollback.md) if the 5xx rate exceeds **1%** or P99 exceeds **2 seconds**
  for **5 minutes**, if integrity diverges, or if a severe user-visible fault appears sooner.
  The existing latency alarm is stricter (P99 >1 second); investigate it even when the
  release rollback threshold has not been reached.

Close the change only after the new SHA, image digest, smoke result, integrity result, and
metrics window are recorded. See [GitHub's manual workflow procedure](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
