locals {
  web_acl_name               = "${var.name_prefix}-waf"
  web_acl_metric_name        = "${var.name_prefix}-waf"
  common_rules_metric_name   = "${var.name_prefix}-aws-common-rules"
  upload_rules_metric_name   = "${var.name_prefix}-aws-common-rules-upload"
  common_rules_managed_name  = "AWSManagedRulesCommonRuleSet"
  common_rules_managed_owner = "AWS"
  upload_path                = "/api/v1/upload"
  upload_method              = "POST"
}

resource "aws_wafv2_web_acl" "this" {
  name        = local.web_acl_name
  description = "AWS managed common protections for ${var.name_prefix}"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "AWS-AWSManagedRulesCommonRuleSet"
    priority = 0

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = local.common_rules_managed_name
        vendor_name = local.common_rules_managed_owner

        # Preserve the managed body-size block everywhere except the one endpoint whose
        # application contract intentionally accepts a much larger body.
        scope_down_statement {
          not_statement {
            statement {
              and_statement {
                statement {
                  byte_match_statement {
                    positional_constraint = "EXACTLY"
                    search_string         = local.upload_path

                    field_to_match {
                      uri_path {}
                    }

                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }

                statement {
                  byte_match_statement {
                    positional_constraint = "EXACTLY"
                    search_string         = local.upload_method

                    field_to_match {
                      method {}
                    }

                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = local.common_rules_metric_name
      sampled_requests_enabled   = var.sampled_requests_enabled
    }
  }

  rule {
    name     = "AWS-AWSManagedRulesCommonRuleSet-Upload"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = local.common_rules_managed_name
        vendor_name = local.common_rules_managed_owner

        # The API accepts uploads up to 50 MB, while this managed rule blocks
        # every body over 8 KiB on ALB. Count that one rule and keep every
        # other CommonRuleSet action enforced. The application owns the 50 MB
        # limit and the WAF still inspects the body portion available to it.
        rule_action_override {
          name = "SizeRestrictions_BODY"

          action_to_use {
            count {}
          }
        }

        scope_down_statement {
          and_statement {
            statement {
              byte_match_statement {
                positional_constraint = "EXACTLY"
                search_string         = local.upload_path

                field_to_match {
                  uri_path {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }

            statement {
              byte_match_statement {
                positional_constraint = "EXACTLY"
                search_string         = local.upload_method

                field_to_match {
                  method {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = local.upload_rules_metric_name
      sampled_requests_enabled   = var.sampled_requests_enabled
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.web_acl_metric_name
    sampled_requests_enabled   = var.sampled_requests_enabled
  }

  tags = merge(
    var.tags,
    {
      Component = "waf"
      ManagedBy = "Terraform"
    },
  )
}

# Keep the policy and its attachment in one module so a caller cannot create
# an unattached Web ACL. AWS requires this resource to be in the same region as
# the protected ALB.
resource "aws_wafv2_web_acl_association" "this" {
  resource_arn = var.protected_resource_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
