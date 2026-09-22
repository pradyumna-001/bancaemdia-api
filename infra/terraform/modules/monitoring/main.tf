locals {
  roles         = toset(["api", "extraction", "materialization", "beat"])
  alarm_actions = [var.alarm_topic_arn == null ? aws_sns_topic.infrastructure[0].arn : var.alarm_topic_arn]
}

resource "aws_sns_topic" "infrastructure" {
  count = var.alarm_topic_arn == null ? 1 : 0
  name  = "${var.name}-infrastructure-alarms"
  tags  = var.tags
}

resource "aws_cloudwatch_log_group" "service" {
  for_each          = local.roles
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.environment == "production" ? 90 : 30
  tags              = var.tags
}

resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each       = aws_cloudwatch_log_group.service
  name           = "${var.name}-${each.key}-errors"
  log_group_name = each.value.name
  pattern        = "{ $.level = \"error\" }"
  metric_transformation {
    name      = "${var.name}-${each.key}-errors"
    namespace = "BancaEmDia/${var.environment}"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy" {
  alarm_name          = "${var.name}-api-unhealthy"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  dimensions          = { TargetGroup = var.target_group_arn_suffix, LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name}-rds-storage-low"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 21474836480
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${var.name}-operations"
  dashboard_body = jsonencode({
    widgets = [
      { type = "metric", x = 0, y = 0, width = 12, height = 6, properties = {
        title   = "ECS CPU", region = var.region, stat = "Average",
        metrics = [["AWS/ECS", "CPUUtilization", "ClusterName", var.cluster_name, "ServiceName", var.api_service_name]]
      } },
      { type = "metric", x = 12, y = 0, width = 12, height = 6, properties = {
        title   = "RDS free storage", region = var.region, stat = "Minimum",
        metrics = [["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", var.db_identifier]]
      } },
    ]
  })
}
