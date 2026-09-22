variable "name" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "dr_vpc_id" { type = string }
variable "dr_private_subnet_ids" { type = list(string) }
variable "tags" { type = map(string) }
