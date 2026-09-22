variable "name_prefix" {
  description = "Short, environment-qualified prefix used for the Web ACL and CloudWatch metrics."
  type        = string

  validation {
    condition = (
      length(var.name_prefix) >= 1 &&
      length(var.name_prefix) <= 100 &&
      can(regex("^[A-Za-z0-9_-]+$", var.name_prefix))
    )
    error_message = "name_prefix must contain 1-100 letters, numbers, underscores, or hyphens."
  }
}

variable "protected_resource_arn" {
  description = "ARN of the regional Application Load Balancer protected by this Web ACL."
  type        = string

  validation {
    condition = (
      can(regex("^arn:(aws|aws-us-gov|aws-cn):elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/app/[^/]+/[A-Za-z0-9]+$", var.protected_resource_arn))
    )
    error_message = "protected_resource_arn must be an Application Load Balancer ARN."
  }
}

variable "sampled_requests_enabled" {
  description = "Whether AWS WAF may retain request samples. Keep disabled unless the data-retention implications have been reviewed."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags to apply to the Web ACL."
  type        = map(string)
  default     = {}
}
