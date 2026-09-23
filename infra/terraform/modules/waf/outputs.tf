output "web_acl_arn" {
  description = "ARN of the regional AWS WAFv2 Web ACL."
  value       = aws_wafv2_web_acl.this.arn
}

output "web_acl_id" {
  description = "ID of the regional AWS WAFv2 Web ACL."
  value       = aws_wafv2_web_acl.this.id
}

output "protected_resource_arn" {
  description = "ARN of the Application Load Balancer associated with the Web ACL."
  value       = aws_wafv2_web_acl_association.this.resource_arn
}
