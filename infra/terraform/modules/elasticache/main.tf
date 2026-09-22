resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-redis"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

# Celery/Kombu's Redis transport uses multiple logical DBs and a non-cluster
# client. Cluster mode enabled would redirect commands and break the broker.
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.name}-redis"
  description                = "Highly available Celery broker and cache"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.r6g.large"
  port                       = 6379
  num_node_groups            = 1
  replicas_per_node_group    = 1
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [var.security_group_id]
  snapshot_retention_limit   = 7
  apply_immediately          = false
  tags                       = var.tags
}
