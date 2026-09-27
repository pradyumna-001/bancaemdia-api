# Operations runbooks

Use the runbook for the action at hand. Record the environment, immutable commit SHA,
incident or change ticket, operator, start time, and previous release before changing anything.
Never put database URLs, bearer tokens, or customer data in a ticket or chat.

| Procedure | When to use it |
| --- | --- |
| [Lightsail Phase 1](runbooks/lightsail-phase1.md) | Launch on the approved low-cost single-host architecture |
| [Deploy](runbooks/deploy.md) | Later ECS/RDS phase: promote through staging and production |
| [CD setup](runbooks/cd.md) | Configure OIDC, GitHub environments, canary and image promotion |
| [Rollback](runbooks/rollback.md) | Restore the previous application version after a failed release |
| [Migration](runbooks/migration.md) | Apply and verify an Alembic schema change |
| [Incident response](runbooks/incident.md) | Triage alarms, assign severity, mitigate, and close an incident |
| [Scaling](runbooks/scaling.md) | Add or remove ECS capacity; plan RDS or Redis class changes |

Related procedures: [failure injection](chaos-testing.md) and
[dashboard view refresh](runbooks/painel-refresh.md).

## Current deployment boundary

The administrator approved [one Lightsail host for Phase 1](runbooks/lightsail-phase1.md).
The separate [ECS/RDS Terraform](../infra/terraform/README.md) and its CD workflow are
reserved for later scale. No AWS environment has been applied or verified yet.

The current API exposes `/health`, `/ready`, and an authenticated `/api/v1/painel`. It does
not expose `/auth/login`; smoke tests must use the approved synthetic JWT setup and never
assume a login endpoint exists. `/ready` requires primary PostgreSQL, replica, and Redis;
Anthropic and queue depth are visible as report-only checks and need separate inspection.
