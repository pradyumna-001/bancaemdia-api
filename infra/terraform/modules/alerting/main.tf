locals {
  critical_alarm_actions = [aws_sns_topic.critical.arn]
  warning_alarm_actions  = [aws_sns_topic.warning.arn]

  common_tags = merge(
    var.tags,
    {
      Component = "alerting"
      ManagedBy = "Terraform"
    },
  )

  # PromQL alarms query metrics ingested through the CloudWatch OTLP endpoint.
  # Every expression reduces to one series, as required by CloudWatch alarms.
  alarm_policies = {
    API_P99_Latency = {
      query           = <<-PROMQL
        histogram_quantile(0.99, sum(rate(http_request_duration_seconds{service="${var.service_name}",environment="${var.environment}"}[5m]))) > 1
      PROMQL
      pending_period  = 300
      recovery_period = 300
      severity        = "warning"
      alarm_summary   = "API request latency p99 exceeded 1 second for five minutes."
    }
    API_Error_Rate = {
      query           = <<-PROMQL
        (sum(rate(http_requests_total{service="${var.service_name}",environment="${var.environment}",status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total{service="${var.service_name}",environment="${var.environment}"}[5m])), 1e-9)) > 0.01
      PROMQL
      pending_period  = 300
      recovery_period = 300
      severity        = "critical"
      alarm_summary   = "The API 5xx error rate exceeded one percent for five minutes."
    }
    Replica_Lag = {
      query           = <<-PROMQL
        max(pg_replication_lag_seconds{service="${var.service_name}",environment="${var.environment}",role="replica"} >= 0) > 30
      PROMQL
      pending_period  = 300
      recovery_period = 300
      severity        = "critical"
      alarm_summary   = "PostgreSQL replica lag exceeded 30 seconds for five minutes."
    }
    Extraction_Queue_Depth = {
      query           = <<-PROMQL
        max(celery_queue_depth{service="${var.service_name}",environment="${var.environment}",queue="extraction"}) > 100
      PROMQL
      pending_period  = 600
      recovery_period = 300
      severity        = "warning"
      alarm_summary   = "The extraction queue held more than 100 jobs for ten minutes."
    }
    Materialization_Queue_Depth = {
      query           = <<-PROMQL
        max(celery_queue_depth{service="${var.service_name}",environment="${var.environment}",queue="materialization"}) > 50
      PROMQL
      pending_period  = 600
      recovery_period = 300
      severity        = "warning"
      alarm_summary   = "The materialization queue held more than 50 jobs for ten minutes."
    }
    Postgres_Disk_Full = {
      query           = <<-PROMQL
        sum(increase(materialization_failed_total{service="${var.service_name}",environment="${var.environment}",reason="disk_full"}[5m])) > 0
      PROMQL
      pending_period  = 0
      recovery_period = 300
      severity        = "critical"
      alarm_summary   = "PostgreSQL rejected a materialization write because storage is full."
    }
    Circuit_Breaker_Open = {
      query           = <<-PROMQL
        max(circuit_breaker_state{service="${var.service_name}",environment="${var.environment}",breaker="anthropic"} == bool 1) > 0.5
      PROMQL
      pending_period  = 0
      recovery_period = 60
      severity        = "critical"
      alarm_summary   = "The Anthropic circuit breaker is open."
    }
    Anthropic_Daily_Cost = {
      query           = <<-PROMQL
        sum(increase(anthropic_cost_usd_total{service="${var.service_name}",environment="${var.environment}"}[24h])) >= ${var.anthropic_daily_cost_limit_usd * 0.8}
      PROMQL
      pending_period  = 0
      recovery_period = 300
      severity        = "warning"
      alarm_summary   = "Anthropic spend exceeded 80 percent of the configured rolling daily limit."
    }
    Anthropic_Daily_Cost_Limit = {
      query           = <<-PROMQL
        sum(increase(anthropic_cost_usd_total{service="${var.service_name}",environment="${var.environment}"}[24h])) >= ${var.anthropic_daily_cost_limit_usd}
      PROMQL
      pending_period  = 0
      recovery_period = 300
      severity        = "critical"
      alarm_summary   = "Anthropic spend exceeded the configured rolling daily limit."
    }
    Revisao_Pendente_Spike = {
      query           = <<-PROMQL
        sum(increase(revisao_pendente_created_total{service="${var.service_name}",environment="${var.environment}"}[1h])) > 100
      PROMQL
      pending_period  = 0
      recovery_period = 300
      severity        = "warning"
      alarm_summary   = "More than 100 pending reviews were created in one hour."
    }
    DLQ_Depth = {
      query           = <<-PROMQL
        max(celery_queue_depth{service="${var.service_name}",environment="${var.environment}",queue="dead_letter"}) > 0
      PROMQL
      pending_period  = 300
      recovery_period = 300
      severity        = "critical"
      alarm_summary   = "The dead-letter queue was non-empty for five minutes."
    }
  }
}

resource "aws_sns_topic" "critical" {
  name = "${var.name_prefix}-alerts-critical"

  tags = merge(local.common_tags, { Severity = "critical" })
}

resource "aws_sns_topic" "warning" {
  name = "${var.name_prefix}-alerts-warning"

  tags = merge(local.common_tags, { Severity = "warning" })
}

resource "aws_sns_topic_subscription" "warning_email" {
  topic_arn = aws_sns_topic.warning.arn
  protocol  = "email"
  endpoint  = var.warning_email_endpoint
}

# User authorization remains required and the explicit read-only guardrail
# avoids Chatbot's unsafe AdministratorAccess default.
resource "aws_iam_role" "chatbot_notifications" {
  name = substr("${var.name_prefix}-chatbot-notifications", 0, 64)

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "chatbot.amazonaws.com"
        }
      },
    ]
  })

  tags = local.common_tags
}

# AWS documents these read-only CloudWatch actions as the minimum policy for
# forwarding and formatting alarm notifications. The role cannot mutate AWS
# resources, and the channel guardrail below independently caps permissions.
resource "aws_iam_policy" "chatbot_notifications" {
  name = "${var.name_prefix}-notifications"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchNotificationDetails"
        Effect = "Allow"
        Action = [
          "cloudwatch:Describe*",
          "cloudwatch:Get*",
          "cloudwatch:List*",
        ]
        Resource = "*"
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "chatbot_notifications" {
  role       = aws_iam_role.chatbot_notifications.name
  policy_arn = aws_iam_policy.chatbot_notifications.arn
}

resource "aws_chatbot_slack_channel_configuration" "critical" {
  configuration_name = "${var.name_prefix}-critical-alerts"
  iam_role_arn       = aws_iam_role.chatbot_notifications.arn
  slack_channel_id   = var.slack_channel_id
  slack_team_id      = var.slack_workspace_id
  sns_topic_arns     = [aws_sns_topic.critical.arn]

  guardrail_policy_arns = [
    aws_iam_policy.chatbot_notifications.arn,
  ]
  logging_level               = "ERROR"
  user_authorization_required = true

  tags = local.common_tags

  depends_on = [aws_iam_role_policy_attachment.chatbot_notifications]
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarm_policies

  alarm_name        = "${var.name_prefix}-${each.key}"
  alarm_description = "${each.value.alarm_summary} Runbook: ${var.runbook_url}"
  actions_enabled   = true

  evaluation_interval = 60

  evaluation_criteria {
    promql_criteria {
      query           = trimspace(each.value.query)
      pending_period  = each.value.pending_period
      recovery_period = each.value.recovery_period
    }
  }

  alarm_actions = each.value.severity == "critical" ? local.critical_alarm_actions : local.warning_alarm_actions
  ok_actions    = each.value.severity == "critical" ? local.critical_alarm_actions : local.warning_alarm_actions

  tags = merge(local.common_tags, { Severity = each.value.severity })
}
