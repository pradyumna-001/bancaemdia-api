variable "name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "api_service_name" { type = string }
variable "db_identifier" { type = string }
variable "target_group_arn_suffix" { type = string }
variable "alb_arn_suffix" { type = string }
variable "alarm_topic_arn" {
  type    = string
  default = null
}
variable "tags" { type = map(string) }
