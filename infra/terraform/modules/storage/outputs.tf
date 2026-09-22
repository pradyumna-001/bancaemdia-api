output "bucket_names" { value = { for key, bucket in aws_s3_bucket.this : key => bucket.id } }
output "bucket_arns" { value = { for key, bucket in aws_s3_bucket.this : key => bucket.arn } }
output "alb_logs_bucket" { value = aws_s3_bucket.this["alb"].id }
output "alb_policy_id" { value = aws_s3_bucket_policy.alb.id }
