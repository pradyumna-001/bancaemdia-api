variable "name_prefix" {
  description = "Short, environment-qualified prefix used for alarms and notification resources."
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

variable "service_name" {
  description = "Value of the service label used to scope every PromQL query."
  type        = string
  default     = "bancaemdia"

  validation {
    condition = (
      length(var.service_name) >= 1 &&
      length(var.service_name) <= 100 &&
      can(regex("^[A-Za-z0-9_.-]+$", var.service_name))
    )
    error_message = "service_name must contain 1-100 letters, numbers, dots, underscores, or hyphens."
  }
}

variable "environment" {
  description = "Value of the environment label used to isolate alarm contributors."
  type        = string

  validation {
    condition = (
      length(var.environment) >= 1 &&
      length(var.environment) <= 100 &&
      can(regex("^[A-Za-z0-9_.-]+$", var.environment))
    )
    error_message = "environment must contain 1-100 letters, numbers, dots, underscores, or hyphens."
  }
}

variable "anthropic_daily_cost_limit_usd" {
  description = "Daily Anthropic spend limit in USD; the alarm threshold is 80 percent of this value."
  type        = number

  validation {
    condition     = var.anthropic_daily_cost_limit_usd > 0
    error_message = "anthropic_daily_cost_limit_usd must be greater than zero."
  }
}

variable "warning_email_endpoint" {
  description = "Email address subscribed to warning notifications. AWS requires the recipient to confirm the subscription."
  type        = string
  sensitive   = true

  validation {
    condition = can(regex(
      "^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$",
      var.warning_email_endpoint,
    ))
    error_message = "warning_email_endpoint must be a valid email address."
  }
}

variable "slack_workspace_id" {
  description = "ID of the Slack workspace already authorized in AWS Chatbot/Amazon Q Developer."
  type        = string

  validation {
    condition     = can(regex("^T[A-Z0-9]+$", var.slack_workspace_id))
    error_message = "slack_workspace_id must be an uppercase Slack workspace ID beginning with T."
  }
}

variable "slack_channel_id" {
  description = "ID of the Slack channel that receives critical alarm notifications."
  type        = string

  validation {
    condition     = can(regex("^[CG][A-Z0-9]+$", var.slack_channel_id))
    error_message = "slack_channel_id must be an uppercase Slack channel ID beginning with C or G."
  }
}

variable "runbook_url" {
  description = "Public or operator-accessible HTTPS URL for the incident runbook."
  type        = string
  default     = "https://github.com/pradyumna-001/bancaemdia-api/blob/main/docs/runbooks/incident.md"

  validation {
    condition = can(regex(
      "^https://.+/docs/runbooks/incident[.]md([?#].*)?$",
      var.runbook_url,
    ))
    error_message = "runbook_url must be an HTTPS URL ending in /docs/runbooks/incident.md."
  }
}

variable "tags" {
  description = "Additional tags to apply to supported alerting resources."
  type        = map(string)
  default     = {}
}
