terraform {
  required_version = ">= 1.14.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.46.0, < 7.0.0"
    }
  }

  backend "s3" {
    key          = "lightsail/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}

resource "aws_lightsail_instance" "app" {
  name              = "bancaemdia-phase1"
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  key_pair_name     = var.key_pair_name
  ip_address_type   = "ipv4"

  tags = {
    Project = "bancaemdia"
    Phase   = "1"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_lightsail_static_ip" "app" {
  name = "bancaemdia-phase1-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.name
  instance_name  = aws_lightsail_instance.app.name
}

# This is the complete public firewall: database, Redis and API port 8000 stay private.
resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = [var.operator_cidr]
  }

  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = ["0.0.0.0/0"]
  }
}

# Lightsail object storage speaks the S3 API. Instance attachment avoids access keys
# in Terraform state. The bucket is private and cannot be destroyed while non-empty.
resource "aws_lightsail_bucket" "data" {
  name         = var.bucket_name
  bundle_id    = var.bucket_bundle_id
  force_delete = false

  tags = {
    Project = "bancaemdia"
    Phase   = "1"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_lightsail_bucket_resource_access" "app" {
  bucket_name   = aws_lightsail_bucket.data.name
  resource_name = aws_lightsail_instance.app.name
}

# Whole-account budget: restricting it to a tag would miss untagged charges.
resource "aws_budgets_budget" "phase1" {
  name         = "bancaemdia-phase1-monthly"
  budget_type  = "COST"
  limit_amount = "50"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
