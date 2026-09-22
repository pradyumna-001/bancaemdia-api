output "alarm_arns" {
  description = "CloudWatch PromQL alarm ARNs keyed by the canonical alarm name."
  value       = { for name, alarm in aws_cloudwatch_metric_alarm.this : name => alarm.arn }
}

output "alarm_names" {
  description = "CloudWatch PromQL alarm names keyed by the canonical alarm name."
  value       = { for name, alarm in aws_cloudwatch_metric_alarm.this : name => alarm.alarm_name }
}

output "critical_sns_topic_arn" {
  description = "SNS topic ARN used by critical alarms and the Slack channel configuration."
  value       = aws_sns_topic.critical.arn
}

output "warning_sns_topic_arn" {
  description = "SNS topic ARN used by warning alarms and the email subscription."
  value       = aws_sns_topic.warning.arn
}

output "slack_chat_configuration_arn" {
  description = "ARN of the AWS Chatbot/Amazon Q Developer Slack channel configuration."
  value       = aws_chatbot_slack_channel_configuration.critical.chat_configuration_arn
}
