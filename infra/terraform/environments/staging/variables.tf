variable "region" { type = string }
variable "dr_region" { type = string }
variable "api_domain" { type = string }
variable "route53_zone_id" { type = string }
variable "image_uri" {
  type    = string
  default = ""
}
variable "deploy_enabled" {
  type    = bool
  default = false
}
variable "notification" {
  type = object({
    warning_email                  = string
    slack_workspace_id             = string
    slack_channel_id               = string
    anthropic_daily_cost_limit_usd = number
  })
  default = null
}
variable "tags" {
  type    = map(string)
  default = {}
}
