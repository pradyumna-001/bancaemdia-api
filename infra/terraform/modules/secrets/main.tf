locals {
  names = toset([
    "database-primary-url", "database-replica-url", "jwt-secret-key",
    "coleta-token-secret", "anthropic-api-key", "upload-webhook-secret",
  ])
}

resource "aws_secretsmanager_secret" "runtime" {
  for_each                = local.names
  name                    = "bancaemdia/${var.environment}/${each.key}"
  description             = "${var.environment} runtime secret. Value is set outside Terraform."
  recovery_window_in_days = 30
  tags                    = merge(var.tags, { Component = "runtime-secret" })
}
