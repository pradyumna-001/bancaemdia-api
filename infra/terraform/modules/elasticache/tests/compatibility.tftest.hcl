mock_provider "aws" {}

run "celery_compatible_failover" {
  command = plan
  variables {
    name               = "bancaemdia-staging"
    private_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
    security_group_id  = "sg-0123456789abcdef0"
    tags               = {}
  }
  assert {
    condition = (
      aws_elasticache_replication_group.this.num_node_groups == 1 &&
      aws_elasticache_replication_group.this.replicas_per_node_group == 1 &&
      aws_elasticache_replication_group.this.automatic_failover_enabled &&
      aws_elasticache_replication_group.this.at_rest_encryption_enabled &&
      aws_elasticache_replication_group.this.transit_encryption_enabled
    )
    error_message = "Redis must be single-shard, highly available and encrypted for the current Celery client."
  }
}
