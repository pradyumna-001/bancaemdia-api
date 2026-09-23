output "primary_endpoint" { value = aws_elasticache_replication_group.this.primary_endpoint_address }
output "replication_group_id" { value = aws_elasticache_replication_group.this.id }
output "cache_configuration_endpoint" { value = aws_elasticache_replication_group.cache_cluster.configuration_endpoint_address }
