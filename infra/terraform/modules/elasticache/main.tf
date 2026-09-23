resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-redis"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

# Celery/Kombu retains a dedicated Redis broker. Its transport uses logical
# databases and cannot speak Redis Cluster.
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.name}-redis"
  description                = "Highly available Celery broker and result backend"
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

resource "aws_elasticache_replication_group" "cache_cluster" {
  replication_group_id       = "${var.name}-cache-cluster"
  description                = "Two-shard application cache and distributed rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.r6g.large"
  port                       = 6379
  num_node_groups            = 2
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
