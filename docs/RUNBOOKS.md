# Operations runbooks

Use the runbook for the action at hand. Record the environment, immutable commit SHA,
incident or change ticket, operator, start time, and previous release before changing anything.
Never put database URLs, bearer tokens, or customer data in a ticket or chat.

| Procedure | When to use it |
| --- | --- |
| [Deploy](runbooks/deploy.md) | Promote a reviewed release through staging and production |
| [Rollback](runbooks/rollback.md) | Restore the previous application version after a failed release |
| [Migration](runbooks/migration.md) | Apply and verify an Alembic schema change |
| [Incident response](runbooks/incident.md) | Triage alarms, assign severity, mitigate, and close an incident |
| [Scaling](runbooks/scaling.md) | Add or remove ECS capacity; plan RDS or Redis class changes |

Related procedures: [failure injection](chaos-testing.md) and
[dashboard view refresh](runbooks/painel-refresh.md).

## Current deployment boundary

The repository has CI, a Docker image, and [Terraform staging/production definitions](../infra/terraform/README.md).
No AWS environment has been applied or verified yet. `.github/workflows/cd.yml`, image promotion,
and the migration deployment process remain in
[issue #42](https://github.com/pradyumna-001/bancaemdia-api/issues/42).
The `gh workflow run cd.yml` commands below describe the required operator interface; they
cannot be run until that workflow is merged into the default branch with `workflow_dispatch`
and the named inputs. Reconcile these runbooks with the delivered workflow and resource names
before the first deployment. GitHub [requires the dispatchable workflow on the default
branch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

The current API exposes `/health`, `/ready`, and an authenticated `/api/v1/painel`. It does
not expose `/auth/login`; smoke tests must use the approved synthetic JWT setup and never
assume a login endpoint exists. `/ready` requires primary PostgreSQL, replica, and Redis;
Anthropic and queue depth are visible as report-only checks and need separate inspection.
