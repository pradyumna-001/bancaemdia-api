data "aws_caller_identity" "current" {}

locals {
  buckets = {
    uploads = "${var.name}-uploads-${data.aws_caller_identity.current.account_id}"
    exports = "${var.name}-exports-${data.aws_caller_identity.current.account_id}"
    backups = "${var.name}-backups-${data.aws_caller_identity.current.account_id}"
    alb     = "${var.name}-alb-logs-${data.aws_caller_identity.current.account_id}"
  }
}

resource "aws_s3_bucket" "this" {
  for_each      = local.buckets
  bucket        = each.value
  force_destroy = false
  tags          = merge(var.tags, { Component = each.key })
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "alb" {
  bucket = aws_s3_bucket.this["alb"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowALBLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "logdelivery.elasticloadbalancing.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.this["alb"].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.this["alb"].arn, "${aws_s3_bucket.this["alb"].arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

resource "aws_s3_bucket_policy" "data" {
  for_each = { for key, bucket in aws_s3_bucket.this : key => bucket if key != "alb" }
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
