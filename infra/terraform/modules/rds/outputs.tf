output "primary_endpoint" { value = aws_db_instance.primary.address }
output "read_endpoint" { value = aws_db_instance.read.address }
output "primary_identifier" { value = aws_db_instance.primary.identifier }
output "read_identifier" { value = aws_db_instance.read.identifier }
output "dr_identifier" { value = aws_db_instance.cross_region.identifier }
output "managed_master_secret_arn" { value = aws_db_instance.primary.master_user_secret[0].secret_arn }
