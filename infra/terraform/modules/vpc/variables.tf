variable "name" { type = string }
variable "cidr" { type = string }
variable "azs" {
  type = list(string)
  validation {
    condition     = length(var.azs) == 2 && length(distinct(var.azs)) == 2
    error_message = "Exactly two distinct availability zones are required."
  }
}
variable "public_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.public_cidrs) == 2
    error_message = "Two public CIDRs are required."
  }
}
variable "private_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.private_cidrs) == 2
    error_message = "Two private CIDRs are required."
  }
}
variable "enable_nat" { type = bool }
variable "tags" { type = map(string) }
