resource "aws_lb" "this" {
  name                       = "${var.name}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [var.security_group_id]
  subnets                    = var.public_subnet_ids
  enable_deletion_protection = true
  access_logs {
    bucket  = var.logs_bucket
    enabled = true
  }
  tags = var.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path                = "/ready"
    protocol            = "HTTP"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 10
  }
  deregistration_delay = 30
  tags                 = var.tags
}

resource "aws_lb_target_group" "canary" {
  name        = "${var.name}-canary"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path                = "/ready"
    protocol            = "HTTP"
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 10
  }
  deregistration_delay = 30
  tags                 = var.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type = "forward"
    forward {
      target_group {
        arn    = aws_lb_target_group.api.arn
        weight = 100
      }
      target_group {
        arn    = aws_lb_target_group.canary.arn
        weight = 0
      }
    }
  }
  lifecycle {
    # The release workflow owns traffic weights after initial provisioning.
    ignore_changes = [default_action]
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
