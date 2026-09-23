# Respond to an incident

**Scope:** API, PostgreSQL, Redis/Celery, and Anthropic. Critical alarms route to Slack;
warnings route to SNS email. PagerDuty and phone escalation still need configuration.

## Classify and escalate

| Severity | Examples | First action |
| --- | --- | --- |
| SEV1 | Data loss/corruption, outage, unsafe writes | Page on-call, appoint commander, stop harmful writes |
| SEV2 | Major degradation or growing backlog | Page on-call and assign mitigation owner |
| SEV3 | Limited degradation or isolated alert | Triage and schedule follow-up |

Escalate **Slack → PagerDuty → phone** until acknowledged. Keep the roster, contact details,
and stakeholder channel in the private operations directory, not Git. Skip missing integrations
and record severity, owner, timeline, and next update.

## Triage without losing evidence

1. Confirm environment, alarm history, and the `service`/`environment` filtered metric graph.
   Missing telemetry is not recovery.
2. Check `/health` and `/ready`. PostgreSQL primary, replica, and Redis are required;
   Anthropic and queue depth are **report-only**. Check release SHA, workers, and DLQ.
3. Correlate `X-Request-ID` with structured logs and trace ID in the private log group:

   ```text
   fields @timestamp, event, request_id, trace_id, error_type
   | filter request_id = "REQUEST_ID"
   | sort @timestamp asc
   ```

   Narrow the time window; keep tokens, payloads, and user IDs out of incident chat.

## Mitigate the observed cause

| Signal | Check | First safe response |
| --- | --- | --- |
| `Replica_Lag` >30s | Replay, CPU, storage | Use primary for critical reads; do not promote blindly |
| `Extraction_Queue_Depth` >100 | Workers, provider 429s, breaker | Restore consumers/provider; avoid extra paid calls |
| `Materialization_Queue_Depth` >50 | Workers, DB locks/storage | Fix DB constraint before scaling workers |
| `Circuit_Breaker_Open` | Provider 5xx and retries | Keep degraded mode until provider recovers |
| `Postgres_Disk_Full` | Free storage, failed writes | Reduce writes; engage DB owner |
| `DLQ_Depth` >0 | Original task, exception, payload pattern | Fix cause; replay idempotently and retain evidence |
| API 5xx/P99 | Route, dependency, recent SHA | [Roll back](rollback.md) a correlated release |

Use [scaling.md](scaling.md) only after locating the bottleneck. Redis fallback does not
prove Celery has resumed consuming.

## Recover and close

Confirm readiness, smoke tests, queue drain, replica lag, breaker closure, and an alarm
recovery window. For financial impact run `python scripts/conferir_numeros.py --todos`
with the primary DB role. Retain DLQ and event evidence until reconciled. Notify stakeholders;
record impact, timeline, integrity result, root cause, and follow-up owner.

Before launch, test metric ingestion and notification delivery in staging. CloudWatch evaluates
these alarms each minute. Extraction/materialization queues require a ten-minute breach; DLQ
requires five. Account for this delay when measuring response time.
