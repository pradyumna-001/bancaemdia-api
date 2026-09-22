output "api_url" { value = "https://${var.api_domain}" }
output "alb_dns_name" { value = module.alb.dns_name }
output "ecs_cluster_name" { value = module.ecs.cluster_name }
output "ecs_service_names" { value = module.ecs.service_names }
output "rds_primary_endpoint" { value = module.rds.primary_endpoint }
output "rds_read_endpoint" { value = module.rds.read_endpoint }
output "rds_dr_identifier" { value = module.rds.dr_identifier }
output "rds_master_secret_arn" {
  value     = module.rds.managed_master_secret_arn
  sensitive = true
}
output "redis_endpoint" { value = module.elasticache.primary_endpoint }
output "bucket_names" { value = module.storage.bucket_names }
output "ecr_repository_url" { value = aws_ecr_repository.api.repository_url }
output "runtime_secret_arns" { value = module.secrets.arns }
