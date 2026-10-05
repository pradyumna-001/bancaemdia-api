variable "region" { type = string }

variable "create_legacy_lock_table" {
  type        = bool
  description = "Create the DynamoDB lock table required by the later ECS/RDS environments. Lightsail Phase 1 uses S3 lockfiles instead."
  default     = true
}
