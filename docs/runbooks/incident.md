# Alert incident response

This runbook covers the Week 3 CloudWatch alarms for the API, PostgreSQL replica, queues,
Anthropic integration, pending reviews, and dead-letter queue. It is intentionally short; the
Week 4 operations work can extend it without changing alarm links.

## Before the first incident

- Confirm the API and every worker metrics endpoint is scraped by an OpenTelemetry Collector.
  The collector must add the expected `service` and `environment` metric labels and export to
  the regional CloudWatch OTLP metrics endpoint with short-lived AWS credentials.
- In CloudWatch Query Studio, run each query from the Terraform alerting module and confirm it
  returns only the intended environment. Metrics endpoints must remain private to the collector.
- Authorize the Slack workspace in Amazon Q Developer in chat applications, apply the channel
  configuration, and confirm the warning email subscription sent by SNS.
- Exercise the notification route in staging. A mocked Terraform test proves configuration shape,
  not delivery through AWS, Slack, or email.

## First response

1. Acknowledge the alarm and name an incident owner. Record the alarm name, transition time,
   environment, and recent deployment; never paste request bodies, tokens, credentials, or user
   identifiers into Slack or email.
2. Open the alarm history and its PromQL query. Verify the signal still breaches and is not a
   missing, unavailable (`pg_replication_lag_seconds = -1`), or cross-environment series.
3. Correlate the time window with structured logs and traces. Use request IDs internally and keep
   user-level details out of the incident channel.
4. Stop further harm before optimizing recovery: roll back a suspect release, reduce intake, or
   add worker capacity according to the alarm guidance below.
5. Keep the alarm enabled. Silence or threshold changes require an incident note and a follow-up
   change reviewed after recovery.

## Alarm guidance

| Alarm | First checks | Safe first mitigation |
| --- | --- | --- |
| `API_P99_Latency` | Slow paths, DB/Redis latency, downstream spans, saturation | Roll back a correlated release or reduce optional work |
| `API_Error_Rate` | 5xx by route, exception class, dependency health, latest deployment | Roll back the failing release; preserve logs and traces |
| `Replica_Lag` | WAL receiver/replay state, network, replica CPU/storage | Force freshness-critical reads to primary and recover the replica; do not promote it blindly |
| `Extraction_Queue_Depth` | Extraction workers, Anthropic latency/rate limits, breaker state | Restore or scale extraction consumers without increasing provider pressure |
| `Materialization_Queue_Depth` | Worker health, DB locks/timeouts, failed tasks | Fix DB contention or scale materialization consumers |
| `Circuit_Breaker_Open` | Anthropic status, credentials, timeouts, recent failure types | Keep degraded behavior active; do not bypass the breaker |
| `Anthropic_Daily_Cost` | Cost by user/model, retry volume, cache hit rate, prompt changes | Pause non-essential extraction and investigate the cost source |
| `Revisao_Pendente_Spike` | New reviews by reason, parser/prompt release, affected source | Stop the faulty ingestion path while preserving review records |
| `DLQ_Depth` | Failed task names, original queue, exception classes, poison payload pattern | Fix the cause before replay; retain DLQ messages for audit |

## Recovery and closure

- Confirm the signal remains healthy for the alarm's configured recovery period. A notification
  delivery alone is not evidence of recovery.
- Verify queues are draining, the circuit breaker is closed, replica lag is current, and no new
  DLQ or review spike is accumulating.
- Reconcile any paused or replayed work using the application's idempotent workflow. Never delete
  DLQ entries merely to clear the alarm.
- Record impact, timeline, root cause, mitigation, and follow-up owner. Tune a threshold only with
  production evidence; do not tune away a real incident.

## Notification smoke test

In staging, `aws cloudwatch set-alarm-state` can validate the alarm-to-SNS-to-Slack/email route.
Restore the state after the message arrives. This does not validate the PromQL expression, metric
ingestion, or dwell period; validate those separately in Query Studio with a controlled signal.

CloudWatch evaluates the alarms every minute. Conditions with a five-minute pending period can
transition shortly after five minutes; queue alarms intentionally require ten minutes. Therefore,
the general “under five minutes” target applies only to immediate alarms such as an open circuit
breaker, not to policies whose required dwell time is already five or ten minutes.
