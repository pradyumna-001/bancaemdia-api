locals {
  name = "bancaemdia-${var.environment}"
  tags = merge(var.tags, {
    Project     = "bancaemdia"
    Environment = var.environment
    ManagedBy   = "Terraform"
  })
  primary_cidr = var.environment == "staging" ? "10.40.0.0/16" : "10.50.0.0/16"
  dr_cidr      = var.environment == "staging" ? "10.41.0.0/16" : "10.51.0.0/16"
  octet        = var.environment == "staging" ? 40 : 50
  dr_octet     = var.environment == "staging" ? 41 : 51
}

data "aws_availability_zones" "primary" { state = "available" }
data "aws_availability_zones" "dr" {
  provider = aws.dr
  state    = "available"
}

resource "terraform_data" "deployment_gate" {
  input = var.deploy_enabled
  lifecycle {
    precondition {
      condition     = !var.deploy_enabled || can(regex("^.+@sha256:[a-f0-9]{64}$", var.image_uri))
      error_message = "Enabling ECS services requires an immutable image URI ending in @sha256:<64 hex>."
    }
  }
}

module "vpc" {
  source        = "./modules/vpc"
  name          = local.name
  cidr          = local.primary_cidr
  azs           = slice(data.aws_availability_zones.primary.names, 0, 2)
  public_cidrs  = [for i in range(2) : "10.${local.octet}.${i}.0/24"]
  private_cidrs = [for i in range(2) : "10.${local.octet}.${i + 10}.0/24"]
  enable_nat    = true
  tags          = local.tags
}

module "vpc_dr" {
  source        = "./modules/vpc"
  providers     = { aws = aws.dr }
  name          = "${local.name}-dr"
  cidr          = local.dr_cidr
  azs           = slice(data.aws_availability_zones.dr.names, 0, 2)
  public_cidrs  = [for i in range(2) : "10.${local.dr_octet}.${i}.0/24"]
  private_cidrs = [for i in range(2) : "10.${local.dr_octet}.${i + 10}.0/24"]
  enable_nat    = false
  tags          = local.tags
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public HTTPS entry point"
  vpc_id      = module.vpc.vpc_id
  tags        = local.tags
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "Private Fargate tasks"
  vpc_id      = module.vpc.vpc_id
  tags        = local.tags
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "PostgreSQL from application tasks only"
  vpc_id      = module.vpc.vpc_id
  tags        = local.tags
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis TLS from application tasks only"
  vpc_id      = module.vpc.vpc_id
  tags        = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "alb_api" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.ecs.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "ecs_api" {
  security_group_id            = aws_security_group.ecs.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "ecs" {
  security_group_id = aws_security_group.ecs.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
resource "aws_vpc_security_group_ingress_rule" "rds" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.ecs.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = aws_security_group.ecs.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

module "storage" {
  source = "./modules/storage"
  name   = local.name
  tags   = local.tags
}

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the newest 30 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

module "secrets" {
  source      = "./modules/secrets"
  environment = var.environment
  tags        = local.tags
}

module "rds" {
  source                = "./modules/rds"
  providers             = { aws = aws, aws.dr = aws.dr }
  name                  = local.name
  private_subnet_ids    = module.vpc.private_subnet_ids
  security_group_id     = aws_security_group.rds.id
  dr_vpc_id             = module.vpc_dr.vpc_id
  dr_private_subnet_ids = module.vpc_dr.private_subnet_ids
  tags                  = local.tags
}

module "elasticache" {
  source             = "./modules/elasticache"
  name               = local.name
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_id  = aws_security_group.redis.id
  tags               = local.tags
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
  tags = local.tags
}

resource "aws_route53_record" "validation" {
  for_each = {
    for option in aws_acm_certificate.api.domain_validation_options : option.domain_name => {
      name = option.resource_record_name, record = option.resource_record_value, type = option.resource_record_type
    }
  }
  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for record in aws_route53_record.validation : record.fqdn]
}

module "alb" {
  source            = "./modules/alb"
  name              = local.name
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  security_group_id = aws_security_group.alb.id
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn
  logs_bucket       = module.storage.alb_logs_bucket
  tags              = local.tags
  depends_on        = [module.storage]
}

resource "aws_route53_record" "api" {
  zone_id = var.route53_zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = module.alb.dns_name
    zone_id                = module.alb.zone_id
    evaluate_target_health = true
  }
}

module "alerting" {
  count                          = var.notification == null ? 0 : 1
  source                         = "./modules/alerting"
  name_prefix                    = local.name
  environment                    = var.environment
  anthropic_daily_cost_limit_usd = var.notification.anthropic_daily_cost_limit_usd
  warning_email_endpoint         = var.notification.warning_email
  slack_workspace_id             = var.notification.slack_workspace_id
  slack_channel_id               = var.notification.slack_channel_id
  tags                           = local.tags
}

module "monitoring" {
  source                  = "./modules/monitoring"
  name                    = local.name
  environment             = var.environment
  region                  = var.region
  cluster_name            = "${local.name}-ecs"
  api_service_name        = "${local.name}-api"
  db_identifier           = module.rds.primary_identifier
  target_group_arn_suffix = module.alb.target_group_arn_suffix
  alb_arn_suffix          = module.alb.arn_suffix
  alarm_topic_arn         = var.notification == null ? null : module.alerting[0].critical_sns_topic_arn
  tags                    = local.tags
}

module "waf" {
  source                 = "./modules/waf"
  name_prefix            = local.name
  protected_resource_arn = module.alb.arn
  tags                   = local.tags
}

module "ecs" {
  source               = "./modules/ecs"
  name                 = local.name
  environment          = var.environment
  region               = var.region
  private_subnet_ids   = module.vpc.private_subnet_ids
  private_cidrs        = module.vpc.private_cidrs
  security_group_id    = aws_security_group.ecs.id
  api_target_group_arn = module.alb.target_group_arn
  api_hostname         = var.api_domain
  image_uri            = var.image_uri
  deploy_enabled       = var.deploy_enabled
  redis_endpoint       = module.elasticache.primary_endpoint
  jwt_audience         = var.jwt_audience
  jwt_issuer           = var.jwt_issuer
  secret_arns          = module.secrets.arns
  log_group_names      = module.monitoring.log_group_names
  tags                 = local.tags
  depends_on           = [terraform_data.deployment_gate, module.alb]
}
