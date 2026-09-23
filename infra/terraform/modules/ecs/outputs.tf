output "cluster_name" { value = aws_ecs_cluster.this.name }
output "service_names" { value = { for role, service in aws_ecs_service.service : role => service.name } }
output "canary_service_name" { value = var.deploy_enabled ? aws_ecs_service.canary[0].name : null }
