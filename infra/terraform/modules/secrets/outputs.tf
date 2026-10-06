output "arns" { value = { for key, secret in aws_secretsmanager_secret.runtime : key => secret.arn } }
