# Phase 1: one Lightsail host

This is the launch path approved in the [AWS decision](../decisions/aws-initial-budget.md), under the revised R$200/month target. The [Lightsail Terraform root](../../infra/lightsail/README.md) creates the instance, private object bucket and US$50/month budget alert. The ECS/RDS/ALB Terraform and `cd.yml` remain for a later phase. A budget alert is not a spending cap; review actual charges and exchange rates.

## Before installing

- Confirm the actual 4 GiB bundle, bucket bundle, snapshot storage, domain, SSH CIDR and total cost in the chosen account. Apply the Lightsail root only after its plan and cost are reviewed. Do not enable `TF_APPLY_ENABLED` or `CD_ENABLED`.
- Point the domain A record at the static IP. Install Docker Engine with Compose, Python 3.12, PostgreSQL 16 client tools and AWS CLI on the Linux host. Keep the host patched and SSH limited to the operator CIDR; do not open 5432, 6379 or 8000.
- Check that `172.30.84.0/24` does not overlap a host/VPN subnet. Caddy is `172.30.84.2`, the only trusted proxy IP in `runtime.env`. If the subnet changes, change both values together.
- Before moving customer data, resolve the two policy gates in [issue #43](https://github.com/pradyumna-001/bancaemdia-api/pull/130): raw Telegram data ownership/retention and whether environment-injected secrets meet the security rule.

## Install the application

1. Clone the reviewed commit into `/opt/bancaemdia`. Copy `compose.env.example` to `compose.env` and `runtime.env.example` to `runtime.env`. Copy the completed `runtime.env` to `maintenance.env`, then replace both database URLs there with the PostgreSQL migration-owner credential. Create `deploy/lightsail/secrets/postgres_admin_password`. All four are ignored by Git and must be readable only by the operator (`chmod 600` files, `chmod 700` the secrets directory). Fill every placeholder from the approved secret store. Use an **application** DB role only in `runtime.env`. Point both replica URLs to the local primary. Keep `REPROCESS_ADMIN_DATABASE_URL` out of API and worker environments.
2. Pin `APP_IMAGE`, `POSTGRES_IMAGE`, `REDIS_IMAGE` and `CADDY_IMAGE` to reviewed digests. The application digest must be the scanned GHCR image for the reviewed SHA. Authenticate Docker to GHCR using a read-only package token kept outside the repository.
3. Start only PostgreSQL and Redis:

   ```bash
   cd /opt/bancaemdia/deploy/lightsail
   docker compose --env-file compose.env -f compose.yml up -d postgres redis
   ```

4. Run `docker compose --env-file compose.env -f compose.yml --profile ops run --rm migrate` with the maintenance credential. Check `alembic current` against the reviewed migration head. If #130 and #145 have both merged, reconcile their two Alembic branches before this step.
5. From `docker compose ... exec -u postgres postgres psql -U postgres -d bancaemdia`, create a separate `bancaemdia_app` login and set its password interactively with `\password bancaemdia_app`. Grant `USAGE` on schema `public`, `SELECT, INSERT, UPDATE, DELETE` on its application tables, and `USAGE, SELECT` on its sequences. Grant `EXECUTE` on `create_user_profile(text,text)` after #145. Do **not** grant `BYPASSRLS`, superuser, ownership of the private `painel` schema, or `USAGE` on that schema. Verify the role sees another user's profile as zero rows and cannot query `painel.mv_painel_resumo`.
6. Verify bucket access from the host and from a one-off API container, using the attached Lightsail resource access and **no static access key**. Stop if the container cannot obtain credentials. Configure `S3_UPLOAD_BUCKET` only after that succeeds. This setting currently sends new review photos to object storage; Telegram export bytes still occupy `upload_arquivos` in PostgreSQL until worker pickup, so measure WAL/disk headroom for concurrent 50 MiB uploads.
7. Start the rest of the stack and verify TLS:

   ```bash
   docker compose --env-file compose.env -f compose.yml up -d api extraction materialization painel_refresh caddy
   curl --fail https://YOUR_DOMAIN/health
   curl --fail https://YOUR_DOMAIN/ready
   ```

   Inspect the `/ready` details and worker logs. `painel_refresh` uses the maintenance role every 15 seconds; measure refresh duration and freshness before claiming the 30-second dashboard target. Never place its credential in API or workers.

Keep one API process in this phase: recent-write routing marks are process-local. Before adding
API processes or hosts, move those marks to shared storage or provide verified sticky routing.
Monitor `uploads` in `processing` for jobs older than the expected worker window, inspect their
Celery task and `upload_arquivos` row, and alert before retained 50 MiB payloads exhaust disk.
The dashboard known-balance exception is documented in [panel refresh](painel-refresh.md).

## Backups and recovery

- On the host, schedule `python scripts/lightsail_backup.py --bucket PRIVATE_BUCKET --directory /opt/bancaemdia/deploy/lightsail` daily. It creates a PostgreSQL custom-format dump, validates its archive, uploads the dump and SHA-256 sidecar to the private bucket, checks remote size, and deletes the local copy only on success. Alert on a missed run; a failed upload leaves its local dump for inspection. Test AWS CLI access before enabling the schedule.
- Configure the weekly [snapshot workflow](../../.github/workflows/lightsail-snapshot.yml) with a narrowly scoped GitHub OIDC role and `LIGHTSAIL_SNAPSHOT_ENABLED=true` only after the instance exists. Set `LIGHTSAIL_REGION`, `LIGHTSAIL_INSTANCE_NAME` and `LIGHTSAIL_SNAPSHOT_ROLE_ARN`; manually dispatch once, confirm the snapshot and review snapshot storage charges. The workflow does not delete old snapshots automatically.
- Before launch and at least quarterly, run `python scripts/lightsail_restore_drill.py --bucket PRIVATE_BUCKET --backup EXACT_DUMP_NAME --postgres-image REVIEWED_POSTGRES_IMAGE` from an operator machine with sufficient disk and Docker. It verifies the checksum and restores into a disposable container with no published port. Record the result and measured restore time. A passing archive-list check alone is not a restore test.
- The decision accepts RPO 24 hours and RTO of hours. A live restore, DNS recovery and available backups must be demonstrated before claiming those targets.

## Release and rollback

For the first installation, the operator starts the pinned image as above. For later releases, after CI, migration review and a fresh backup, run `python scripts/lightsail_deploy.py ghcr.io/pradyumna-001/bancaemdia-api@sha256:FULL_DIGEST --directory /opt/bancaemdia/deploy/lightsail`. The script pulls the reviewed image, updates API and workers to the same digest, checks public `/ready`, and restores the prior image if the check fails. The schema must remain compatible with the prior image; a binary rollback does not reverse migrations.

Run the existing k6 profile from issue #40 against representative data, check memory/CPU, upload concurrency, dashboard freshness, auth/RLS, the rate-limit fallback alert, budget notification and the restoration result before any traffic. The [issue #44 gate](https://github.com/pradyumna-001/bancaemdia-api/pull/131) still needs live evidence; CI green or Terraform validation is not a production GO.
