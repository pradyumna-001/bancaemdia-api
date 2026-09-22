terraform {
  required_version = ">= 1.14.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.42.0, < 7.0.0"
    }
  }
}

provider "aws" { region = var.region }
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket        = "bancaemdia-tfstate-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = false
  tags          = { Project = "bancaemdia", Purpose = "terraform-state" }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Legacy lock table is provided for older Terraform clients. New runs use the
# S3 lockfile because DynamoDB locking is deprecated in current Terraform.
resource "aws_dynamodb_table" "legacy_lock" {
  name         = "bancaemdia-terraform-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  tags = { Project = "bancaemdia", Purpose = "legacy-terraform-lock" }
}

output "state_bucket" { value = aws_s3_bucket.state.id }
output "legacy_lock_table" { value = aws_dynamodb_table.legacy_lock.name }
