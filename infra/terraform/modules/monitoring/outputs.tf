output "log_group_names" { value = { for role, group in aws_cloudwatch_log_group.service : role => group.name } }
