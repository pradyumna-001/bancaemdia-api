mock_provider "aws" {
}

override_resource {
  target          = aws_sns_topic.critical
  override_during = plan
  values = {
    arn = "arn:aws:sns:us-east-1:123456789012:bancaemdia-staging-alerts-critical"
  }
}

override_resource {
  target          = aws_sns_topic.warning
  override_during = plan
  values = {
    arn = "arn:aws:sns:us-east-1:123456789012:bancaemdia-staging-alerts-warning"
  }
}

override_resource {
  target          = aws_iam_policy.chatbot_notifications
  override_during = plan
  values = {
    arn = "arn:aws:iam::123456789012:policy/bancaemdia-staging-notifications"
  }
}

variables {
  name_prefix                    = "bancaemdia-staging"
  environment                    = "staging"
  anthropic_daily_cost_limit_usd = 125
  warning_email_endpoint         = "on-call@example.com"
  slack_workspace_id             = "T0123456789"
  slack_channel_id               = "C0123456789"
}

run "creates_the_nine_required_promql_alarms" {
  command = plan

  assert {
    condition = toset(keys(aws_cloudwatch_metric_alarm.this)) == toset([
      "API_P99_Latency",
      "API_Error_Rate",
      "Replica_Lag",
      "Extraction_Queue_Depth",
      "Materialization_Queue_Depth",
      "Circuit_Breaker_Open",
      "Anthropic_Daily_Cost",
      "Revisao_Pendente_Spike",
      "DLQ_Depth",
    ])
    error_message = "The module must create exactly the nine required alarms."
  }

  assert {
    condition = alltrue([
      for alarm in values(aws_cloudwatch_metric_alarm.this) : alarm.evaluation_interval == 60
    ])
    error_message = "Every PromQL alarm must evaluate once per minute."
  }

  assert {
    condition = alltrue([
      for alarm in values(aws_cloudwatch_metric_alarm.this) :
      strcontains(one(one(alarm.evaluation_criteria).promql_criteria).query, "service=\"bancaemdia\"") &&
      strcontains(one(one(alarm.evaluation_criteria).promql_criteria).query, "environment=\"staging\"")
    ])
    error_message = "Every PromQL query must be isolated by service and environment."
  }

  assert {
    condition = alltrue([
      for alarm in values(aws_cloudwatch_metric_alarm.this) :
      strcontains(alarm.alarm_description, "/docs/runbooks/incident.md")
    ])
    error_message = "Every alarm description must link to the incident runbook."
  }
}

run "preserves_prometheus_semantics_and_windows" {
  command = plan

  assert {
    condition = (
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).query, "histogram_quantile(0.99") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).query, "sum(rate(http_request_duration_seconds{") &&
      !strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).query, "_bucket") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).query, "rate(") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).query, "> 1") &&
      one(one(aws_cloudwatch_metric_alarm.this["API_P99_Latency"].evaluation_criteria).promql_criteria).pending_period == 300
    )
    error_message = "Latency must use histogram_quantile over native-histogram rates and remain pending for five minutes."
  }

  assert {
    condition = (
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_Error_Rate"].evaluation_criteria).promql_criteria).query, "status=~\"5..\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_Error_Rate"].evaluation_criteria).promql_criteria).query, "/ clamp_min(sum(rate(http_requests_total") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["API_Error_Rate"].evaluation_criteria).promql_criteria).query, "> 0.01") &&
      one(one(aws_cloudwatch_metric_alarm.this["API_Error_Rate"].evaluation_criteria).promql_criteria).pending_period == 300
    )
    error_message = "The API error alarm must use the 5xx-to-total rate ratio for five minutes."
  }

  assert {
    condition = (
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Replica_Lag"].evaluation_criteria).promql_criteria).query, "role=\"replica\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Replica_Lag"].evaluation_criteria).promql_criteria).query, "} >= 0) > 30") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Replica_Lag"].evaluation_criteria).promql_criteria).query, "> 30") &&
      one(one(aws_cloudwatch_metric_alarm.this["Replica_Lag"].evaluation_criteria).promql_criteria).pending_period == 300 &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Extraction_Queue_Depth"].evaluation_criteria).promql_criteria).query, "queue=\"extraction\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Extraction_Queue_Depth"].evaluation_criteria).promql_criteria).query, "> 100") &&
      one(one(aws_cloudwatch_metric_alarm.this["Extraction_Queue_Depth"].evaluation_criteria).promql_criteria).pending_period == 600 &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Materialization_Queue_Depth"].evaluation_criteria).promql_criteria).query, "queue=\"materialization\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Materialization_Queue_Depth"].evaluation_criteria).promql_criteria).query, "> 50") &&
      one(one(aws_cloudwatch_metric_alarm.this["Materialization_Queue_Depth"].evaluation_criteria).promql_criteria).pending_period == 600
    )
    error_message = "Replica and queue alarms must preserve the required thresholds and pending periods."
  }

  assert {
    condition = (
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Circuit_Breaker_Open"].evaluation_criteria).promql_criteria).query, "breaker=\"anthropic\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Circuit_Breaker_Open"].evaluation_criteria).promql_criteria).query, "max(circuit_breaker_state{") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Circuit_Breaker_Open"].evaluation_criteria).promql_criteria).query, "} == bool 1) > 0.5") &&
      one(one(aws_cloudwatch_metric_alarm.this["Circuit_Breaker_Open"].evaluation_criteria).promql_criteria).pending_period == 0 &&
      one(one(aws_cloudwatch_metric_alarm.this["Circuit_Breaker_Open"].evaluation_criteria).promql_criteria).recovery_period == 60 &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["DLQ_Depth"].evaluation_criteria).promql_criteria).query, "queue=\"dead_letter\"") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["DLQ_Depth"].evaluation_criteria).promql_criteria).query, "> 0") &&
      one(one(aws_cloudwatch_metric_alarm.this["DLQ_Depth"].evaluation_criteria).promql_criteria).pending_period == 300
    )
    error_message = "Circuit-breaker and DLQ alarms must detect their exact states within the required windows."
  }

  assert {
    condition = (
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Anthropic_Daily_Cost"].evaluation_criteria).promql_criteria).query, "increase(anthropic_cost_usd_total") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Anthropic_Daily_Cost"].evaluation_criteria).promql_criteria).query, "[24h]") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Anthropic_Daily_Cost"].evaluation_criteria).promql_criteria).query, "> 100") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Revisao_Pendente_Spike"].evaluation_criteria).promql_criteria).query, "increase(revisao_pendente_created_total") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Revisao_Pendente_Spike"].evaluation_criteria).promql_criteria).query, "[1h]") &&
      strcontains(one(one(aws_cloudwatch_metric_alarm.this["Revisao_Pendente_Spike"].evaluation_criteria).promql_criteria).query, "> 100")
    )
    error_message = "Cost and pending-review alarms must use counter-aware rolling increases."
  }
}

run "routes_critical_to_slack_and_warnings_to_email" {
  command = plan

  assert {
    condition = (
      alltrue([
        for name in ["API_Error_Rate", "Replica_Lag", "Circuit_Breaker_Open", "DLQ_Depth"] :
        toset(aws_cloudwatch_metric_alarm.this[name].alarm_actions) == toset([aws_sns_topic.critical.arn])
      ]) &&
      alltrue([
        for name in ["API_P99_Latency", "Extraction_Queue_Depth", "Materialization_Queue_Depth", "Anthropic_Daily_Cost", "Revisao_Pendente_Spike"] :
        toset(aws_cloudwatch_metric_alarm.this[name].alarm_actions) == toset([aws_sns_topic.warning.arn])
      ]) &&
      alltrue([
        for alarm in values(aws_cloudwatch_metric_alarm.this) :
        toset(alarm.ok_actions) == toset(alarm.alarm_actions)
      ])
    )
    error_message = "Every alarm and recovery action must use its severity-specific SNS topic."
  }

  assert {
    condition = (
      toset(aws_chatbot_slack_channel_configuration.critical.sns_topic_arns) == toset([aws_sns_topic.critical.arn]) &&
      aws_chatbot_slack_channel_configuration.critical.slack_team_id == var.slack_workspace_id &&
      aws_chatbot_slack_channel_configuration.critical.slack_channel_id == var.slack_channel_id &&
      aws_chatbot_slack_channel_configuration.critical.user_authorization_required == true &&
      toset(aws_chatbot_slack_channel_configuration.critical.guardrail_policy_arns) == toset([aws_iam_policy.chatbot_notifications.arn]) &&
      aws_chatbot_slack_channel_configuration.critical.logging_level == "ERROR"
    )
    error_message = "The critical topic must feed the intended least-privilege Slack channel configuration."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.chatbot_notifications.assume_role_policy).Statement[0].Principal.Service == "chatbot.amazonaws.com" &&
      aws_iam_role_policy_attachment.chatbot_notifications.role == aws_iam_role.chatbot_notifications.name &&
      aws_iam_role_policy_attachment.chatbot_notifications.policy_arn == aws_iam_policy.chatbot_notifications.arn &&
      jsondecode(aws_iam_policy.chatbot_notifications.policy).Statement[0].Effect == "Allow" &&
      jsondecode(aws_iam_policy.chatbot_notifications.policy).Statement[0].Resource == "*" &&
      toset(jsondecode(aws_iam_policy.chatbot_notifications.policy).Statement[0].Action) == toset([
        "cloudwatch:Describe*",
        "cloudwatch:Get*",
        "cloudwatch:List*",
      ])
    )
    error_message = "The Slack role and guardrail must use only the minimum notification permissions."
  }

  assert {
    condition = (
      aws_sns_topic_subscription.warning_email.topic_arn == aws_sns_topic.warning.arn &&
      aws_sns_topic_subscription.warning_email.protocol == "email"
    )
    error_message = "The warning topic must have an email subscription."
  }
}

run "rejects_a_non_positive_daily_limit" {
  command = plan

  variables {
    anthropic_daily_cost_limit_usd = 0
  }

  expect_failures = [
    var.anthropic_daily_cost_limit_usd,
  ]
}

run "rejects_a_runbook_at_the_wrong_path" {
  command = plan

  variables {
    runbook_url = "https://example.com/not-the-runbook.md"
  }

  expect_failures = [
    var.runbook_url,
  ]
}

run "rejects_invalid_scope_labels" {
  command = plan

  variables {
    service_name = "unsafe\"label"
    environment  = ""
  }

  expect_failures = [
    var.service_name,
    var.environment,
  ]
}

run "rejects_invalid_slack_identifiers" {
  command = plan

  variables {
    slack_workspace_id = "workspace-name"
    slack_channel_id   = "channel-name"
  }

  expect_failures = [
    var.slack_workspace_id,
    var.slack_channel_id,
  ]
}
