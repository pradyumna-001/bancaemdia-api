resource "aws_db_subnet_group" "primary" {
  name       = "${var.name}-db"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

resource "aws_db_parameter_group" "primary" {
  name   = "${var.name}-postgres16"
  family = "postgres16"
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pg_cron"
    apply_method = "pending-reboot"
  }
  parameter {
    name         = "cron.database_name"
    value        = "bancaemdia"
    apply_method = "pending-reboot"
  }
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }
  parameter {
    name         = "ssl_min_protocol_version"
    value        = "TLSv1.2"
    apply_method = "immediate"
  }
  tags = var.tags
}

resource "aws_db_instance" "primary" {
  identifier                      = "${var.name}-primary"
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = "db.r6g.large"
  db_name                         = "bancaemdia"
  username                        = "bancaemdia_admin"
  manage_master_user_password     = true
  allocated_storage               = 200
  max_allocated_storage           = 500
  storage_type                    = "gp3"
  storage_encrypted               = true
  multi_az                        = true
  backup_retention_period         = 35
  backup_window                   = "03:00-04:00"
  maintenance_window              = "Sun:04:00-Sun:05:00"
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-primary-final"
  db_subnet_group_name            = aws_db_subnet_group.primary.name
  parameter_group_name            = aws_db_parameter_group.primary.name
  vpc_security_group_ids          = [var.security_group_id]
  publicly_accessible             = false
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  tags                            = var.tags
}

resource "aws_db_instance" "read" {
  identifier                 = "${var.name}-read"
  replicate_source_db        = aws_db_instance.primary.identifier
  instance_class             = "db.r6g.large"
  storage_type               = "gp3"
  db_subnet_group_name       = aws_db_subnet_group.primary.name
  vpc_security_group_ids     = [var.security_group_id]
  publicly_accessible        = false
  deletion_protection        = true
  skip_final_snapshot        = true
  auto_minor_version_upgrade = true
  tags                       = var.tags
}

resource "aws_db_subnet_group" "dr" {
  provider   = aws.dr
  name       = "${var.name}-dr-db"
  subnet_ids = var.dr_private_subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "dr" {
  provider    = aws.dr
  name        = "${var.name}-dr-db"
  description = "No application access to disaster recovery replica"
  vpc_id      = var.dr_vpc_id
  tags        = var.tags
}

resource "aws_kms_key" "dr" {
  provider            = aws.dr
  description         = "Cross-region RDS replica for ${var.name}"
  enable_key_rotation = true
  tags                = var.tags
}

resource "aws_db_instance" "cross_region" {
  provider               = aws.dr
  identifier             = "${var.name}-dr"
  replicate_source_db    = aws_db_instance.primary.arn
  instance_class         = "db.r6g.large"
  storage_type           = "gp3"
  storage_encrypted      = true
  kms_key_id             = aws_kms_key.dr.arn
  db_subnet_group_name   = aws_db_subnet_group.dr.name
  vpc_security_group_ids = [aws_security_group.dr.id]
  publicly_accessible    = false
  deletion_protection    = true
  skip_final_snapshot    = true
  tags                   = var.tags
}
