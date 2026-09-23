locals {
  roles = {
    api             = { cpu = 1024, memory = 2048, count = 2, command = null, log = "api" }
    extraction      = { cpu = 2048, memory = 4096, count = 4, command = ["celery", "-A", "bancaemdia.workers.celery_app:app", "worker", "-Q", "extraction", "--concurrency=4", "--loglevel=INFO"], log = "extraction" }
    materialization = { cpu = 1024, memory = 2048, count = 8, command = ["celery", "-A", "bancaemdia.workers.celery_app:app", "worker", "-Q", "materialization", "--concurrency=2", "--loglevel=INFO"], log = "materialization" }
    beat            = { cpu = 512, memory = 1024, count = 1, command = ["celery", "-A", "bancaemdia.workers.celery_app:app", "beat", "--loglevel=INFO"], log = "beat" }
  }
  redis_base      = "rediss://${var.redis_endpoint}:6379"
  cache_base      = "rediss://${var.redis_cache_endpoint}:6379"
  telemetry_roles = toset(["api", "extraction", "materialization"])
  collector_config = {
    for role in local.telemetry_roles : role => yamlencode({
      receivers = {
        otlp = { protocols = { grpc = { endpoint = "127.0.0.1:4317" } } }
        prometheus = { config = { scrape_configs = [{
          job_name        = role
          scrape_interval = "15s"
          static_configs = [{
            targets = [role == "api" ? "127.0.0.1:8000" : "127.0.0.1:9100"]
            labels  = { service = "bancaemdia", environment = var.environment }
          }]
        }] } }
      }
      processors = { batch = {} }
      exporters = {
        "otlphttp/metrics" = {
          metrics_endpoint = "https://monitoring.${var.region}.amazonaws.com/v1/metrics"
          auth             = { authenticator = "sigv4auth/metrics" }
        }
        "otlphttp/traces" = {
          traces_endpoint = "https://xray.${var.region}.amazonaws.com/v1/traces"
          auth            = { authenticator = "sigv4auth/traces" }
        }
      }
      extensions = {
        "sigv4auth/metrics" = { region = var.region, service = "monitoring" }
        "sigv4auth/traces"  = { region = var.region, service = "xray" }
      }
      service = {
        extensions = ["sigv4auth/metrics", "sigv4auth/traces"]
        pipelines = {
          metrics = { receivers = ["prometheus"], processors = ["batch"], exporters = ["otlphttp/metrics"] }
          traces  = { receivers = ["otlp"], processors = ["batch"], exporters = ["otlphttp/traces"] }
        }
      }
    })
  }
  common_environment = [
    { name = "APP_ENV", value = var.environment },
    { name = "JWT_ALGORITHM", value = "RS256" },
    { name = "JWT_AUDIENCE", value = var.jwt_audience },
    { name = "JWT_ISSUER", value = var.jwt_issuer },
    { name = "REDIS_URL", value = "${local.cache_base}/0?ssl_cert_reqs=required" },
    { name = "REDIS_CLUSTER_MODE", value = "true" },
    { name = "S3_UPLOAD_BUCKET", value = var.upload_bucket_name },
    { name = "CELERY_BROKER_URL", value = "${local.redis_base}/0?ssl_cert_reqs=required" },
    { name = "CELERY_RESULT_BACKEND", value = "${local.redis_base}/1?ssl_cert_reqs=required" },
    { name = "RATE_LIMIT_STORAGE", value = "redis+cluster://${var.redis_cache_endpoint}:6379" },
    { name = "RATE_LIMIT_TRUSTED_PROXY_CIDRS", value = join(",", var.private_cidrs) },
    { name = "API_INTERNAL_URL", value = "https://${var.api_hostname}" },
    { name = "WORKER_METRICS_PORT", value = "9100" },
    { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4317" },
  ]
  common_secrets = [
    { name = "DATABASE_URL", valueFrom = var.secret_arns["database-primary-url"] },
    { name = "DATABASE_URL_REPLICA", valueFrom = var.secret_arns["database-replica-url"] },
    { name = "JWT_SECRET_KEY", valueFrom = var.secret_arns["jwt-secret-key"] },
    { name = "COLETA_TOKEN_SECRET", valueFrom = var.secret_arns["coleta-token-secret"] },
    { name = "ANTHROPIC_API_KEY", valueFrom = var.secret_arns["anthropic-api-key"] },
    { name = "UPLOAD_WEBHOOK_SECRET", valueFrom = var.secret_arns["upload-webhook-secret"] },
  ]
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name}-ecs"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = var.tags
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "secrets" {
  name = "${var.name}-read-runtime-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = values(var.secret_arns)
    }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "media" {
  name = "${var.name}-media-objects"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = "${var.upload_bucket_arn}/media/*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "telemetry" {
  role       = aws_iam_role.task.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_ecs_task_definition" "service" {
  for_each                 = { for role, config in local.roles : role => config if var.deploy_enabled }
  family                   = "${var.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode(concat([merge({
    name         = each.key
    image        = var.image_uri
    essential    = true
    environment  = concat(local.common_environment, contains(["extraction", "materialization"], each.key) ? [{ name = "PROMETHEUS_MULTIPROC_DIR", value = "/tmp/prometheus" }] : [])
    secrets      = local.common_secrets
    portMappings = each.key == "api" ? [{ containerPort = 8000, hostPort = 8000, protocol = "tcp" }] : []
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = var.log_group_names[each.value.log]
        awslogs-region        = var.region
        awslogs-stream-prefix = "ecs"
      }
    }
    }, each.value.command == null ? {} : { command = each.value.command })], contains(local.telemetry_roles, each.key) ? [{
    name        = "otel-collector"
    image       = "public.ecr.aws/aws-observability/aws-otel-collector:v0.49.0"
    essential   = true
    environment = [{ name = "AOT_CONFIG_CONTENT", value = local.collector_config[each.key] }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = var.log_group_names[each.value.log]
        awslogs-region        = var.region
        awslogs-stream-prefix = "otel"
      }
    }
  }] : []))
  tags = var.tags
}

resource "aws_ecs_service" "service" {
  for_each        = { for role, config in local.roles : role => config if var.deploy_enabled }
  name            = "${var.name}-${each.key}"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = each.value.count
  capacity_provider_strategy {
    capacity_provider = contains(["api", "beat"], each.key) ? "FARGATE" : "FARGATE_SPOT"
    weight            = 1
  }
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = false
  }
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = var.api_target_group_arn
      container_name   = "api"
      container_port   = 8000
    }
  }
  health_check_grace_period_seconds = each.key == "api" ? 120 : null
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  lifecycle {
    # Deployments pin task revisions; Terraform only owns service topology.
    ignore_changes = [task_definition]
  }
  depends_on = [aws_ecs_cluster_capacity_providers.this, aws_iam_role_policy_attachment.execution, aws_iam_role_policy.secrets]
  tags       = var.tags
}

resource "aws_ecs_service" "canary" {
  count           = var.deploy_enabled ? 1 : 0
  name            = "${var.name}-api-canary"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.service["api"].arn
  desired_count   = 0
  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = var.canary_target_group_arn
    container_name   = "api"
    container_port   = 8000
  }
  health_check_grace_period_seconds = 120
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
  depends_on = [aws_ecs_cluster_capacity_providers.this, aws_iam_role_policy_attachment.execution, aws_iam_role_policy.secrets]
  tags       = var.tags
}
