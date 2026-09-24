variable "region" {
  type = string
}

variable "availability_zone" {
  type = string
}

variable "blueprint_id" {
  type        = string
  description = "Choose an active Linux blueprint returned by aws lightsail get-blueprints."
}

variable "bundle_id" {
  type        = string
  description = "Choose the Linux 4 GiB public-IPv4 bundle returned by aws lightsail get-bundles."
}

variable "key_pair_name" {
  type = string
}

variable "operator_cidr" {
  type        = string
  description = "Single trusted public IPv4 CIDR for SSH, e.g. 198.51.100.4/32."

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0)) && var.operator_cidr != "0.0.0.0/0"
    error_message = "operator_cidr must be a restricted IPv4 CIDR."
  }
}

variable "bucket_name" {
  type        = string
  description = "Globally unique private Lightsail object-storage bucket name."
}

variable "bucket_bundle_id" {
  type        = string
  description = "Select a bucket plan with aws lightsail get-bucket-bundles; budget its capacity."
}

variable "budget_email" {
  type        = string
  description = "Operator address for account-wide monthly budget notifications."
}
