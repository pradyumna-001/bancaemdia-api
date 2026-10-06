# Scale production capacity

**Owner:** on-call engineer with infrastructure reviewer. The ECS services, RDS instance,
and ElastiCache replication group described here are planned in
[issue #41](https://github.com/pradyumna-001/bancaemdia-api/issues/41). Confirm the actual
resource names and Terraform state before any change. Record current size, desired size,
reason, cost estimate, and reversal time in the change ticket.

## Identify the bottleneck

Use a sustained CPU >70% as an investigation trigger, not automatic permission to scale.
Compare request rate, P99, DB connections/locks, memory, and CPU per service. The current
queue alarms trigger at **extraction >100** and **materialization >50** for ten minutes;
the roadmap's generic queue >100 threshold does not replace the materialization limit.
Replica lag >30 seconds needs replica and write-load investigation before adding API tasks.
Do not scale extraction into Anthropic 429s or an open circuit breaker, or scale
materialization into PostgreSQL storage/lock failures.

## Horizontal ECS change

For the selected service, read its current desired/running count, task definition, and
deployment status. Use its actual environment-qualified name; `api`, `extraction-workers`,
and `materialization-workers` are planned roles, not confirmed AWS resource IDs.

```bash
aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$SERVICE" \
  --query 'services[0].{desired:desiredCount,running:runningCount,task:taskDefinition,status:status}'
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$SERVICE" --desired-count "$NEW_COUNT"
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$SERVICE"
```

Coordinate the target count with Terraform or an autoscaling policy so a later apply does
not silently undo it. Increase one service at a time and watch task placement, errors,
queue age/depth, DB connections, provider limits, and P99 for at least one alarm window.
Return to the recorded prior count when demand subsides, then confirm stability again.
The [ECS update-service CLI](https://docs.aws.amazon.com/cli/latest/reference/ecs/update-service.html)
accepts `--desired-count`; that change does not by itself create a new software deployment.

## Vertical database or cache change

- **RDS:** inspect CPU, free storage, connections, IOPS, replica lag, pending modifications,
  Multi-AZ status, and backup health. Rehearse the proposed instance class in staging,
  make a snapshot, and apply via the reviewed Terraform change during a maintenance window.
  Changing the RDS DB instance class causes an outage; `apply immediately` can also apply
  other pending changes. See [AWS RDS scheduling](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_ModifyInstance.ApplyImmediately.html).
- **Redis:** inspect memory, evictions, CPU, connections, shard/replica topology, and broker
  queue depth. Change node type or shard/replica count through the reviewed infrastructure
  configuration after a staging rehearsal. A node-type change can briefly disconnect
  clients during DNS cutover even when the cluster remains online. See
  [ElastiCache scaling](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Scaling.RedisReplGrps.html).

After either change, verify `/ready`, a synthetic write and read, queue drain, replica lag,
and `python scripts/conferir_numeros.py --todos` if database writes were affected.

## Estimate cost before approval

Use the target Region and current contract prices in the
[AWS Pricing Calculator](https://docs.aws.amazon.com/pricing-calculator/latest/userguide/what-is-pricing-calculator.html).
For ECS, estimate added task-hours × (vCPU-hour rate × task vCPU + GB-hour rate × task RAM)
plus data transfer and log ingestion. For RDS and Redis, compare the old and new class-hours
for **all** primary/standby/replica/shard nodes, plus storage, I/O, and backup deltas.
Record monthly and change-window cost, uncertainty, and the date to review scale-down;
do not quote a fixed dollar amount without a current regional estimate.
