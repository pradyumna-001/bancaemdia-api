# Continuous delivery setup and operation

The workflow is `.github/workflows/cd.yml`. AWS resources must be applied and bootstrapped
before setting repository variable `CD_ENABLED=true`. The workflow uses GitHub OIDC, not static
AWS keys. Its trust policy must limit the repository, branch and `staging` / `production`
environment subjects (`staging`, `production`, and `production-rollback`). The role needs ECR image read/write, ECS task definition registration,
ECS service update/describe and one-off task permissions, `iam:PassRole` limited to the ECS
execution/task roles, ELB listener modification and target health reads, and CloudWatch metric
reads. The production role also needs `ecr:DescribeImages` on the staging repository so
the workflow can verify the exact digest already deployed in staging. Scope these to the
environment's ARNs. Protect `production` with required reviewers
and prevent self-approval.

Set environment variables in GitHub:

| Environment | Variables | Secrets |
| --- | --- | --- |
| `staging` | `AWS_REGION`, `CD_AWS_ROLE_ARN`, `CD_ECR_REPOSITORY_URL`, `STAGING_BASE_URL` | `CD_SMOKE_JWT`, `CD_SLACK_WEBHOOK_URL` |
| `production` and `production-rollback` | `AWS_REGION`, `CD_AWS_ROLE_ARN`, `CD_ECR_REPOSITORY_URL`, `PRODUCTION_BASE_URL`, `CD_HTTPS_LISTENER_ARN`, `CD_STABLE_TARGET_GROUP_ARN`, `CD_CANARY_TARGET_GROUP_ARN`, `CD_ALB_ARN_SUFFIX` | `CD_SMOKE_JWT`, `CD_SLACK_WEBHOOK_URL` |

The ALB ARNs and suffix come from Terraform outputs. Use a dedicated synthetic tenant JWT
with no real customer data. The API has no `/auth/login`; the workflow probes `/health`,
`/ready`, and authenticated `/api/v1/painel`. Keep `CD_ENABLED` unset until both AWS
environments are applied with `deploy_enabled=true`, the six runtime secrets are populated,
migrations are complete, and baseline services are healthy. `TF_APPLY_ENABLED` can remain off;
if enabled, coordinate infrastructure applies separately from releases.

On `main` push, the workflow builds the SHA image, creates a Syft SBOM, blocks critical
Grype findings, and pushes to GHCR. The staging job copies that exact image to immutable
ECR, rolls four ECS services at `minimumHealthyPercent=50`, checks `/ready` for 30 seconds,
runs authenticated smoke and a 30-second k6 profile, then executes
`scripts/conferir_numeros.py --todos` as a private one-off Fargate task. The existing full
k6 workflow stays available for a separate load rehearsal.

For production, dispatch a full SHA with `environment=production`. The protected GitHub
environment approval happens before the job accesses production credentials. It verifies
the production ECR digest against the staged ECR digest. If the SHA is
the current `main` head, a second API service starts on the canary target group; the ALB
shifts 10% for 10 minutes, 50% for 5 minutes, then 100% for 5 minutes. Each minute it
checks target health, authenticated HTTP, ALB 5xx rate and P99 latency, and worker error
metrics. The job then updates workers and the stable API to the same digest, runs the
integrity check, shifts traffic back to the stable group, and scales the canary to zero.
The 20-minute observation period leaves time for ECS rollout; the 30-minute acceptance
target still needs a timed staging rehearsal.

An older SHA reachable from `main` selects the rapid rollback path and updates all four
services from their immutable ECR tag without rebuilding. It cancels an in-progress
production promotion and uses the `production-rollback` GitHub environment, which must be
restricted to trusted dispatchers but must not wait for a reviewer. Use:

```bash
gh workflow run cd.yml --ref main -f sha="$PREVIOUS_SHA" -f environment=production
```

The deploy script restores prior task revisions and traffic weights when a normal exception
occurs. Cancellation, runner loss, or a failed restoration requires the incident runbook.
It does not reverse database migrations. Before a production release, review and rehearse
schema changes using [migration.md](migration.md), confirm backward compatibility with the
prior SHA, and record the previously healthy digest. Slack receives deploy start, staging
ready, canary promotion, and rollback events with SHA, author, changed files, and run URL.

The <10-minute staging and <5-minute rollback targets are operational goals; image build,
registry transfer, ECS health and database size can exceed them. Measure end-to-end times
in the first live rehearsal and adjust capacity or process based on those results.
