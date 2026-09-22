# CloudWatch PromQL alerting module

This module creates the nine operational alarms required by Week 3, two SNS
topics, an email subscription for warnings, and an AWS Chatbot (Amazon Q
Developer in chat applications) Slack configuration for critical alerts. It
stores Slack workspace and channel IDs, never a webhook secret.

```hcl
module "alerting" {
  source = "../../modules/alerting"

  name_prefix                    = "bancaemdia-staging"
  environment                    = "staging"
  anthropic_daily_cost_limit_usd = 100
  warning_email_endpoint         = "on-call@example.com"
  slack_workspace_id             = "T0123456789"
  slack_channel_id               = "C0123456789"

  tags = {
    Environment = "staging"
    Service     = "bancaemdia-api"
  }
}
```

The Slack workspace must first be authorized in AWS Chatbot/Amazon Q Developer;
Terraform cannot perform Slack OAuth authorization. The warning email recipient
must confirm the subscription sent by SNS before messages are delivered. The
channel role grants only the CloudWatch `Describe*`, `Get*`, and `List*`
permissions AWS requires to format notifications. It requires per-user
authorization and uses that same narrow customer-managed policy as its explicit
guardrail, so AWS cannot apply the default `AdministratorAccess` guardrail and
user roles cannot expand the channel beyond notification reads.

## Alarm policy

| Alarm | PromQL condition | Pending period | Route |
| --- | --- | --- | --- |
| `API_P99_Latency` | P99 over the 5-minute request histogram > 1 second | 5 minutes | warning email |
| `API_Error_Rate` | 5xx request rate / all request rate > 1% | 5 minutes | critical Slack |
| `Replica_Lag` | replica lag > 30 seconds | 5 minutes | critical Slack |
| `Extraction_Queue_Depth` | extraction depth > 100 | 10 minutes | warning email |
| `Materialization_Queue_Depth` | materialization depth > 50 | 10 minutes | warning email |
| `Circuit_Breaker_Open` | Anthropic breaker state == 1 | immediate | critical Slack |
| `Anthropic_Daily_Cost` | rolling 24-hour increase > 80% of configured limit | immediate | warning email |
| `Revisao_Pendente_Spike` | rolling 1-hour increase > 100 | immediate | warning email |
| `DLQ_Depth` | dead-letter depth > 0 | 5 minutes | critical Slack |

CloudWatch evaluates every query once per minute. Recovery periods are explicit
(one minute for the circuit breaker and five minutes for all other alarms) to
avoid noisy state flapping. Alarm and recovery transitions use the same route.
Every alarm description links to
[`docs/runbooks/incident.md`](../../../../docs/runbooks/incident.md), using
`runbook_url` when an environment hosts that runbook elsewhere.

## Prometheus and CloudWatch contract

These are native CloudWatch PromQL alarms. They require metrics ingested through
the [CloudWatch OTLP endpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html);
classic `PutMetricData` custom metrics cannot satisfy the queries. The runtime
or an OpenTelemetry Collector must scrape/export these Prometheus series while
adding both labels below to every series:

OTLP ingestion and PromQL are not available in every AWS Region. Configure the
provider in a Region listed as supported by CloudWatch and verify availability
before applying the module; the environment stack owns that regional choice.

- `service=<service_name>` (default `bancaemdia`)
- `environment=<environment>` (required, for example `staging`)

Add these as Prometheus target/metric labels, not only as OTel resource
attributes (which CloudWatch exposes with `@resource.*` names). Keep the
Prometheus receiver's default `trim_metric_suffixes: false`, expose
`WORKER_METRICS_PORT` on every worker service, and scrape both the private API
`/metrics` endpoint and every worker endpoint. Otherwise the selectors and
counter names below will not match the ingested series.

The alarms directly consume the existing series and labels:

- OTLP histogram `http_request_duration_seconds` with
  `histogram_quantile(0.99, sum(rate(...[5m])))`
- `http_requests_total{status}` with a 5xx-to-total rate ratio
- `pg_replication_lag_seconds{role="replica"}`
- `celery_queue_depth{queue}`
- `circuit_breaker_state{breaker}`
- `anthropic_cost_usd_total{usuario_id}`, aggregated with `sum(increase(...[24h]))`
- `revisao_pendente_created_total{reason}`, aggregated with
  `sum(increase(...[1h]))`

The Prometheus receiver converts the application's classic `_bucket` family to
one explicit OTLP histogram. CloudWatch then stores it as a native/exponential
histogram under the base name `http_request_duration_seconds`; querying a
`_bucket` suffix after this conversion would return no data. Every expression
reduces to one time series, as CloudWatch requires. `rate` and `increase` handle
counter resets; there is no invalid `quantile="0.99"` label and no summing of
cumulative counter snapshots. If the collector changes label names or stops
delivery, the query returns no contributors and the alarm recovers, so the
environment stack should independently monitor collector health.

Replica lag uses `-1` only as an explicit unavailable sentinel. The alarm query
filters negative samples before aggregation, so an unavailable probe is never
reported as healthy lag and never preserves an older positive value. The gauge
uses Prometheus `mostrecent` mode when a local multiprocess registry is enabled.

The module needs AWS provider 6.42 or newer because that release added
`evaluation_criteria.promql_criteria` to `aws_cloudwatch_metric_alarm`.

## Deployment smoke test

After applying the module, confirming the SNS email subscription, and verifying
OTLP ingestion in CloudWatch:

1. In a non-production environment, inject Anthropic failures until the
   application opens its circuit breaker.
2. Confirm the exported series has value `1` with labels
   `breaker="anthropic"`, `service="bancaemdia"`, and the target environment.
3. Confirm `<name_prefix>-Circuit_Breaker_Open` enters `ALARM` and Slack receives
   the notification in less than five minutes.
4. Remove the failure, allow the breaker to close, and confirm the recovery
   notification after one minute.

The mocked Terraform tests validate policy and routing but cannot prove OTLP
delivery, AWS Chatbot workspace authorization, or Slack delivery.

Run `terraform init -backend=false`, `terraform fmt -check -recursive`,
`terraform validate`, and `terraform test` from this directory. Tests use a
mocked AWS provider and do not contact AWS.

References:

- [CloudWatch PromQL alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-promql.html)
- [Create a PromQL alarm](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Create_PromQL_Alarm.html)
- [Amazon Q Developer Slack setup](https://docs.aws.amazon.com/chatbot/latest/adminguide/slack-setup.html)
- [Amazon Q Developer notification permissions](https://docs.aws.amazon.com/chatbot/latest/adminguide/chatbot-iam-policies.html#chatbot-notifications-policy)
- [Terraform CloudWatch metric alarm](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_metric_alarm)
- [Terraform Chatbot Slack channel configuration](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/chatbot_slack_channel_configuration)
